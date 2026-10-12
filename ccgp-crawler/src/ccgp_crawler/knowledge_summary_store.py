"""Durable summary jobs and reviews, with immutable source snapshots."""
import hashlib
import json
import re
from .bid_models import BidCheckError
from .knowledge_summary_models import SummaryRecord
from .knowledge_skill import skill_snapshot
from .knowledge_models import reference_only
from .knowledge_views import reference_entry,note_view


def dumps(value):return json.dumps(value,ensure_ascii=False)
def fingerprint(value):return hashlib.sha256(dumps(value).encode()).hexdigest()

def material_key(name):
    name=re.sub(r'[\s（）()·:：]', '', name)
    for aliases,canonical in [
        (('营业执照副本','企业营业执照','营业执照复印件'), '营业执照'),
        (('履约设备及技术能力声明函','设备及技术能力声明函','履约设备和专业技术能力声明函'), '设备能力声明函'),
        (('中小企业声明','中小企业声明函（服务）','中小企业声明函（货物）'), '中小企业声明函'),
    ]:
        if name in tuple(re.sub(r'[\s（）()·:：]', '', a) for a in aliases):return canonical
    return name

def record_key(data):
    return (data['kind'],material_key(data['name']),data.get('subject_role','unknown'),
            '' if data.get('subject_role')=='supplier' else data.get('subject',''),data['value'] if data['kind']=='fact' else '')


def prompt_input(value):
    # Originals, full indexed text and provenance stay in the immutable job snapshot.
    # Send the quotation plus nearby text only when needed to recover legacy occurrence.
    quote=value['source']['quote']
    full=value['text'];position=full.find(quote)
    text=full if len(full)<=6000 else quote
    if len(full)>6000 and position>=0:
        text=full[max(0,position-1000):position+len(quote)+1000]
        date=value['data'].get('applicable_date','')
        date_position=full.find(date) if date else -1
        if date_position>=0 and date not in text:
            text+='\n[另一处原文]\n'+full[max(0,date_position-100):date_position+len(date)+100]
    data={k:v for k,v in value['data'].items() if k not in {'quote','source_id'}}
    result={'id':value['id'],'data':data,'text':text,'location':value['source']['location'],
            'ocr':bool(value['source'].get('ocr')),'status':value['status'],'original_quote':quote}
    if value['data']!=value['original']:
        result['human_changes']={k:v for k,v in data.items() if v!=value['original'].get(k)}
    return result

class SummaryStoreMixin:
    def initialize_summaries(self,db):
        db.executescript('''
        CREATE TABLE IF NOT EXISTS knowledge_summaries(id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES runs(id),fingerprint TEXT NOT NULL,status TEXT NOT NULL,config TEXT NOT NULL,inputs TEXT NOT NULL,coverage TEXT NOT NULL,skill TEXT NOT NULL,stop_requested INTEGER NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '',diagnostic TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(run_id,fingerprint));
        CREATE TABLE IF NOT EXISTS summary_parts(summary_id TEXT NOT NULL REFERENCES knowledge_summaries(id),position INTEGER NOT NULL,inputs TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',output TEXT,error TEXT NOT NULL DEFAULT '',PRIMARY KEY(summary_id,position));
        CREATE TABLE IF NOT EXISTS summary_entries(id TEXT PRIMARY KEY,summary_id TEXT NOT NULL REFERENCES knowledge_summaries(id),data TEXT NOT NULL,original TEXT NOT NULL,sources TEXT NOT NULL,entry_ids TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS summary_reviews(id INTEGER PRIMARY KEY,entry_id TEXT NOT NULL,action TEXT NOT NULL,before_data TEXT NOT NULL,after_data TEXT NOT NULL,created_at TEXT NOT NULL);
        ''')
        columns={r[1] for r in db.execute('PRAGMA table_info(documents)')}
        if 'ownership' not in columns:db.execute("ALTER TABLE documents ADD COLUMN ownership TEXT NOT NULL DEFAULT '{}'")
    def coverage(self,run_id):
        with self.connect() as db:return self._coverage(db,run_id)
    def _coverage(self,db,run_id):
        run=db.execute('SELECT r.document_id,d.name FROM runs r JOIN documents d ON r.document_id=d.id WHERE r.id=?',(run_id,)).fetchone()
        batches=db.execute('SELECT position,status,units,error FROM batches WHERE run_id=? ORDER BY position',(run_id,)).fetchall()
        gaps=[{'batch':r['position']+1,'status':r['status'],'locations':list(dict.fromkeys(u['location'] for u in json.loads(r['units']))),'error':r['error']} for r in batches if r['status']!='done']
        ocr=[dict(r) for r in db.execute("SELECT source_id,location,status,error FROM ocr_results WHERE run_id=? AND status NOT IN ('done')",(run_id,))]
        return {'document_id':run['document_id'],'filename':run['name'],'run_id':run_id,'total_batches':len(batches),'completed_batches':sum(r['status']=='done' for r in batches),'failed_batches':sum(r['status']=='failed' for r in batches),'pending_batches':sum(r['status'] not in {'done','failed'} for r in batches),'gaps':gaps,'ocr_gaps':ocr}
    def ownership(self,document_id,confirmed):
        from .knowledge_store import now
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            company=db.execute("SELECT name FROM companies WHERE id='default'").fetchone()[0]
            value={'confirmed':confirmed,'company_name':company if confirmed else '', 'confirmed_at':now() if confirmed else ''}
            if not db.execute('UPDATE documents SET ownership=? WHERE id=?',(dumps(value),document_id)).rowcount:raise KeyError(document_id)
            self._revision(db)
        return self.document(document_id)
    def summary_inputs(self,run_id):
        with self.connect() as db:
            rows=db.execute("SELECT e.*,b.units FROM entries e JOIN batches b ON b.run_id=e.run_id AND b.position=e.position WHERE e.run_id=? AND b.status='done' AND e.status!='rejected' ORDER BY e.position,e.rowid",(run_id,)).fetchall()
        inputs=[]
        for row in rows:
            data=json.loads(row['data']);source=json.loads(row['source'])
            unit=next((u for u in json.loads(row['units']) if u['source_id']==source['source_id']),{})
            text=source.get('ocr_text') or unit.get('text') or source['quote']
            # Evidence chunks never exceed the existing 20k request bound. Full OCR is already indexed in chunks.
            inputs.append({'id':row['id'],'data':data,'original':json.loads(row['original']),'source':source,'text':text,'status':row['status'],'updated_at':row['updated_at']})
        return inputs
    def queue_summary(self,run_id,config):
        from .knowledge_store import uid,now
        inputs=self.summary_inputs(run_id)
        if not inputs:return None
        coverage=self.coverage(run_id)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            document=db.execute('SELECT d.* FROM documents d JOIN runs r ON r.document_id=d.id WHERE r.id=?',(run_id,)).fetchone()
            ownership=json.loads(document['ownership'])
            skill=(config or {}).get('knowledge_skill') or skill_snapshot()
            digest=fingerprint({'inputs':inputs,'coverage':coverage,'ownership':{k:ownership.get(k) for k in ('confirmed','company_name')},'skill_version':skill['version'],'reference_policy':3})
            prior=db.execute('SELECT id FROM knowledge_summaries WHERE run_id=? AND fingerprint=?',(run_id,digest)).fetchone()
            if prior:return prior[0]
            if db.execute("SELECT 1 FROM knowledge_summaries WHERE run_id=? AND status IN ('queued','running','stopping')",(run_id,)).fetchone():raise BidCheckError('请先停止当前汇总')
            # Compact repeated source text by material grouping, with bounded packing.
            groups={}
            for value in inputs:
                if reference_entry(value['data'],value['source']):continue
                key=(value['data']['kind'],material_key(value['data']['name']))
                groups.setdefault(key,[]).append(value)
            ordered=[prompt_input(v) for group in groups.values() for v in group]
            parts=[];current=[]
            for value in ordered:
                if len(dumps([value]))>19000:raise BidCheckError('单条汇总证据超过文本范围，请拆分该条资料后再整理；未截断内容')
                if current and (len(current)>=20 or len(dumps(current+[value]))>19000):parts.append(current);current=[]
                current.append(value)
            if current:parts.append(current)
            summary_id=uid();snapshot={**config,'ownership':ownership}
            db.execute('INSERT INTO knowledge_summaries(id,run_id,fingerprint,status,config,inputs,coverage,skill,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)',(summary_id,run_id,digest,'queued',dumps(snapshot),dumps(inputs),dumps(coverage),dumps(skill),now(),now()))
            db.executemany('INSERT INTO summary_parts(summary_id,position,inputs) VALUES (?,?,?)',((summary_id,i,dumps(v)) for i,v in enumerate(parts)))
        return summary_id
    def summary_raw(self,summary_id):
        with self.connect() as db:row=db.execute('SELECT s.*,r.document_id FROM knowledge_summaries s JOIN runs r ON r.id=s.run_id WHERE s.id=?',(summary_id,)).fetchone()
        if not row:raise KeyError(summary_id)
        result=dict(row)
        for k in ('config','inputs','coverage','skill','diagnostic'):result[k]=json.loads(result[k])
        return result
    def summary_detail(self,summary_id):
        raw=self.summary_raw(summary_id)
        with self.connect() as db:
            counts={r[0]:r[1] for r in db.execute('SELECT status,count(*) FROM summary_parts WHERE summary_id=? GROUP BY status',(summary_id,))}
            entries=db.execute('SELECT * FROM summary_entries WHERE summary_id=? ORDER BY rowid',(summary_id,)).fetchall()
        inputs=raw.pop('inputs');raw['skill']={k:v for k,v in raw['skill'].items() if k!='instructions'}
        raw['config']={k:raw['config'][k] for k in ('model','ownership') if k in raw['config']}
        raw.update(total_parts=sum(counts.values()),completed_parts=counts.get('done',0),
                   failed_parts=counts.get('failed',0),pending_parts=counts.get('pending',0),
                   processed_parts=counts.get('done',0)+counts.get('failed',0))
        with self.connect() as db:raw['part_errors']=self._summary_gaps(db,summary_id)
        raw['reference_clues']=[{'entry_id':value['id'],'name':value['data']['name'],
                               'occurrence':value['data']['occurrence'],
                               'location':value['source']['location'],'quote':value['source']['quote']}
                              for value in inputs if reference_entry(value['data'],value['source'])]
        raw['items']=[{**dict(e),**note_view(json.loads(e['data']).get('notes','')),'data':json.loads(e['data']),'original':json.loads(e['original']),'sources':json.loads(e['sources']),'entry_ids':json.loads(e['entry_ids'])} for e in entries]
        by_id={v['id']:v for v in inputs}
        for item in raw['items']:
            evidence=[by_id.get(i) for i in item['entry_ids']]
            item['reference_only']=bool(reference_entry(item['data']) or evidence and all(v and reference_entry(v['data'],v['source']) for v in evidence))
        return raw
    def summaries(self,document_id=None):
        with self.connect() as db:
            rows=db.execute('SELECT s.id FROM knowledge_summaries s JOIN runs r ON r.id=s.run_id'+(' WHERE r.document_id=?' if document_id else '')+' ORDER BY s.created_at DESC,s.rowid DESC', (document_id,) if document_id else ()).fetchall()
        return [self.summary_detail(r[0]) for r in rows]
    def summary_update(self,summary_id,**fields):
        from .knowledge_store import now
        if not fields.keys()<={'status','diagnostic','error','stop_requested'}:raise ValueError('invalid summary fields')
        fields['updated_at']=now()
        with self.connect() as db:db.execute('UPDATE knowledge_summaries SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',(*fields.values(),summary_id))
    def summary_work(self):
        with self.connect() as db:
            row=db.execute("SELECT id FROM knowledge_summaries WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            return row[0] if row else None
    def summary_claim(self,summary_id):
        with self.connect() as db:return db.execute("UPDATE knowledge_summaries SET status='running' WHERE id=? AND status='queued' AND stop_requested=0",(summary_id,)).rowcount==1
    def summary_next_part(self,summary_id):
        with self.connect() as db:row=db.execute("SELECT * FROM summary_parts WHERE summary_id=? AND status='pending' ORDER BY position LIMIT 1",(summary_id,)).fetchone()
        return {**dict(row),'inputs':json.loads(row['inputs'])} if row else None
    def summary_fail_part(self,summary_id,position,error):
        with self.connect() as db:db.execute("UPDATE summary_parts SET status='failed',error=? WHERE summary_id=? AND position=?",(error,summary_id,position))
    def _summary_gaps(self,db,summary_id):
        parts=db.execute("SELECT position,status,inputs,error FROM summary_parts WHERE summary_id=? AND status!='done' ORDER BY position",(summary_id,)).fetchall()
        return [{'summary_id':summary_id,'part':p['position']+1,'status':p['status'],'error':p['error'],
                 'locations':list(dict.fromkeys(v['location'] for v in json.loads(p['inputs']))),
                 'names':list(dict.fromkeys(v['data']['name'] for v in json.loads(p['inputs'])))} for p in parts]
    def summary_finish_part(self,summary_id,position,output):
        with self.connect() as db:db.execute("UPDATE summary_parts SET status='done',output=?,error='' WHERE summary_id=? AND position=?",(dumps(output.model_dump()),summary_id,position))
    def summary_action(self,summary_id,action):
        from .knowledge_store import now
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT status FROM knowledge_summaries WHERE id=?',(summary_id,)).fetchone()
            if not row:raise KeyError(summary_id)
            if action=='stop':
                if row['status'] not in {'queued','running','stopping'}:raise BidCheckError('当前汇总无需停止')
                db.execute("UPDATE knowledge_summaries SET stop_requested=1,status=?,updated_at=? WHERE id=?",('stopping' if row['status']=='running' else 'stopped',now(),summary_id))
            elif action in {'start','retry'}:
                if row['status'] not in {'failed','stopped','partial'}:raise BidCheckError('当前汇总不可继续或重试')
                db.execute("UPDATE summary_parts SET status='pending',error='' WHERE summary_id=? AND status='failed'",(summary_id,))
                db.execute("UPDATE knowledge_summaries SET stop_requested=0,status='queued',error='',updated_at=? WHERE id=?",(now(),summary_id))
            else:raise BidCheckError('不支持的汇总操作')
        return self.summary_detail(summary_id)
    def summary_commit(self,summary_id):
        from .knowledge_store import uid,now
        raw=self.summary_raw(summary_id);by_id={e['id']:e for e in raw['inputs']}
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            job=db.execute('SELECT status,stop_requested FROM knowledge_summaries WHERE id=?',(summary_id,)).fetchone()
            if job['stop_requested'] or job['status']!='running':raise BidCheckError('汇总已停止')
            if db.execute("SELECT 1 FROM summary_parts WHERE summary_id=? AND status='pending'",(summary_id,)).fetchone():raise BidCheckError('汇总仍有未处理分组')
            outputs=[json.loads(r[0]) for r in db.execute("SELECT output FROM summary_parts WHERE summary_id=? AND status='done' ORDER BY position",(summary_id,))]
            # Deterministic final merge links evidence even when a material spans request parts.
            grouped={}
            for output in outputs:
                for value in output['records']:
                    record=SummaryRecord.model_validate(value).model_dump()
                    if reference_only(record):continue
                    key=(record['kind'],record['category'],material_key(record['name']),record['subject_role'],record['subject'] if record['subject_role']!='supplier' else '',record['value'] if record['kind']=='fact' else '')
                    group=grouped.setdefault(key,record)
                    if group is not record:
                        group['evidence']+=record['evidence'];group['certificates']+=record['certificates']
                        if record['occurrence']=='actual' and group['occurrence']!='actual':
                            for k in ('occurrence','value','evidence_kind','needs_review','notes'):group[k]=record[k]
            current=db.execute('SELECT e.*,EXISTS (SELECT 1 FROM summary_reviews v WHERE v.entry_id=e.id) AS reviewed FROM summary_entries e WHERE summary_id=? ORDER BY rowid DESC',(summary_id,)).fetchall()
            previous=db.execute("SELECT e.* FROM summary_entries e JOIN knowledge_summaries s ON s.id=e.summary_id WHERE s.run_id=? AND EXISTS (SELECT 1 FROM summary_reviews v WHERE v.entry_id=e.id) ORDER BY s.created_at DESC,s.rowid DESC,e.rowid DESC",(raw['run_id'],)).fetchall()
            previous_by_key={}
            for prior in previous:previous_by_key.setdefault(record_key(json.loads(prior['original'])),prior)
            for record in grouped.values():
                if record['kind']=='material' and record['occurrence']=='actual':
                    # A model may mark a redundant directory entry ignored. Still link it
                    # to the actual material, so a reviewed directory warning cannot survive.
                    for source in raw['inputs']:
                        data=source['data']
                        if (data['kind']=='material' and reference_entry(data,source['source'])
                            and material_key(data['name'])==material_key(record['name'])
                            and data.get('subject_role','unknown') in {record['subject_role'],'unknown'}
                            and (not data.get('subject') or data['subject']==record['subject'])):
                            record['evidence'].append({'entry_id':source['id'],'quote':source['source']['quote']})
                prior=previous_by_key.get(record_key(record))
                if prior and prior['status']=='rejected':
                    removed=set(json.loads(prior['entry_ids']))
                    record['evidence']=[e for e in record['evidence'] if e['entry_id'] not in removed]
                    record['certificates']=[c for c in record['certificates'] if not removed.intersection(c['entry_ids'])]
                evidence=list({(v['entry_id'],v['quote']):v for v in record['evidence']}.values())
                if not evidence:continue
                record['evidence']=evidence
                certificates={}
                for cert in record['certificates']:
                    key=(cert['number'],cert['date'])
                    existing=certificates.setdefault(key,cert)
                    existing['entry_ids']=list(dict.fromkeys(existing['entry_ids']+cert['entry_ids']))
                record['certificates']=list(certificates.values())
                sources=[{**by_id[v['entry_id']]['source'],'entry_id':v['entry_id'],'quote':v['quote']} for v in evidence]
                for source in sources:
                    source.update(document_id=raw['document_id'],filename=raw['coverage']['filename'],occurrence=by_id[source['entry_id']]['data']['occurrence'])
                sources.sort(key=lambda s:s.get('occurrence') in {'mention','template'})
                original=dumps(record)
                ids=list(dict.fromkeys(v['entry_id'] for v in evidence))
                prior=previous_by_key.get(record_key(record))
                if prior and prior['status']!='rejected' and set(ids).intersection(json.loads(prior['entry_ids'])):
                    # Carry manual edits into new candidates; never silently overwrite a review.
                    before=json.loads(prior['original']);after=json.loads(prior['data'])
                    for field in ('name','value','subject','category','notes','needs_review','industry','applicable_date','occurrence','subject_role','evidence_kind'):
                        if before.get(field)!=after.get(field):record[field]=after[field]
                # Retrying only failed groups must not recreate or rewrite reviewed
                # candidates from completed groups. Newly linked evidence stays pending.
                if any(e['original']==original for e in current):continue
                pending=next((e for e in current if not e['reviewed'] and record_key(json.loads(e['original']))==record_key(json.loads(original))),None)
                if pending:
                    db.execute('UPDATE summary_entries SET data=?,original=?,sources=?,entry_ids=?,updated_at=? WHERE id=?',(dumps(record),original,dumps(sources),dumps(ids),now(),pending['id']))
                else:
                    db.execute('INSERT INTO summary_entries VALUES (?,?,?,?,?,?,?,?)',(uid(),summary_id,dumps(record),original,dumps(sources),dumps(ids),'pending',now()))
            failures=db.execute("SELECT count(*) FROM summary_parts WHERE summary_id=? AND status='failed'",(summary_id,)).fetchone()[0]
            done=db.execute("SELECT count(*) FROM summary_parts WHERE summary_id=? AND status='done'",(summary_id,)).fetchone()[0]
            pending=db.execute("SELECT 1 FROM summary_entries WHERE summary_id=? AND status='pending'",(summary_id,)).fetchone()
            status=('partial' if done else 'failed') if failures else ('awaiting_review' if pending else 'reviewed')
            error=f'{failures} 个分组未完成，其他分组已处理；可审核成功结果并重试未完成分组' if failures else ''
            db.execute('UPDATE knowledge_summaries SET status=?,error=?,updated_at=? WHERE id=?',(status,error,now(),summary_id))
            if db.execute("SELECT 1 FROM summary_entries WHERE summary_id=? AND status='accepted'",(summary_id,)).fetchone():
                # Refresh coverage for the same confirmed content; immutable older
                # versions/tasks retain their original gaps and evidence snapshots.
                self._revision(db)
    def summary_review(self,updates):
        from .knowledge_store import now
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            snapshots={}
            for update in updates:
                row=db.execute('SELECT e.*,s.status AS job_status,s.run_id,r.document_id,r.generation FROM summary_entries e JOIN knowledge_summaries s ON s.id=e.summary_id JOIN runs r ON r.id=s.run_id WHERE e.id=?',(update['id'],)).fetchone()
                if not row:raise KeyError(update['id'])
                if row['job_status'] not in {'awaiting_review','reviewed','partial'}:raise BidCheckError('汇总尚未完成，暂不可确认')
                if update['status']=='accepted':
                    ids=json.loads(row['entry_ids'])
                    for source_id in ids:
                        source=db.execute('SELECT e.status,e.data,b.status AS batch_status FROM entries e LEFT JOIN batches b ON b.run_id=e.run_id AND b.position=e.position WHERE e.id=?',(source_id,)).fetchone()
                        if not source or source['status']=='rejected':raise BidCheckError('关联来源已剔除，请重新整理已有结果')
                        if source['batch_status']!='done':raise BidCheckError('关联来源批次尚未成功完成，请先重试')
                        if row['summary_id'] not in snapshots:
                            snapshots[row['summary_id']]={e['id']:e['data'] for e in json.loads(db.execute('SELECT inputs FROM knowledge_summaries WHERE id=?',(row['summary_id'],)).fetchone()[0])}
                        before=snapshots[row['summary_id']].get(source_id)
                        if before!=json.loads(source['data']):raise BidCheckError('关联来源已修改，请重新整理已有结果以保留人工修改')
                data=json.loads(row['data'])
                for k,v in update.get('changes',{}).items():
                    if k in {'name','value','subject','category','notes','needs_review','industry','applicable_date','occurrence','subject_role','evidence_kind'}:data[k]=v
                data=SummaryRecord.model_validate(data).model_dump()
                if update['status']=='accepted':
                    sources=[db.execute('SELECT data,source FROM entries WHERE id=?',(i,)).fetchone() for i in json.loads(row['entry_ids'])]
                    if reference_entry(data) or not sources or all(v and reference_entry(json.loads(v['data']),json.loads(v['source'])) for v in sources):
                        raise BidCheckError('目录或空白模板仅作定位线索，请确认正文材料')
                db.execute('UPDATE summary_entries SET data=?,status=?,updated_at=? WHERE id=?',(dumps(data),update['status'],now(),row['id']))
                db.execute('INSERT INTO summary_reviews(entry_id,action,before_data,after_data,created_at) VALUES (?,?,?,?,?)',(row['id'],update['status'],dumps({'data':json.loads(row['data']),'status':row['status']}),dumps({'data':data,'status':update['status']}),now()))
                pending=db.execute("SELECT 1 FROM summary_entries WHERE summary_id=? AND status='pending'",(row['summary_id'],)).fetchone()
                if row['job_status']!='partial':db.execute('UPDATE knowledge_summaries SET status=? WHERE id=?',('awaiting_review' if pending else 'reviewed',row['summary_id']))
                if update['status']=='accepted':db.execute('UPDATE documents SET adopted_run_id=? WHERE id=? AND COALESCE((SELECT generation FROM runs WHERE id=adopted_run_id),0)<=?',(row['run_id'],row['document_id'],row['generation']))
            for run_id in {db.execute('SELECT s.run_id FROM summary_entries e JOIN knowledge_summaries s ON s.id=e.summary_id WHERE e.id=?',(v['id'],)).fetchone()[0] for v in updates}:
                self._refresh_review_state(db,run_id)
            revision=self._revision(db)
        return {'revision_id':revision}
    def _summary_revision_records(self,db,active_only=True):
        rows=db.execute("SELECT e.*,s.run_id,s.config,s.created_at,d.ownership,d.id AS document_id FROM summary_entries e JOIN knowledge_summaries s ON s.id=e.summary_id JOIN runs r ON r.id=s.run_id JOIN documents d ON d.id=r.document_id WHERE (d.active=1 OR ?=0) AND (d.adopted_run_id=r.id OR ?=0) AND (e.status!='pending' OR EXISTS (SELECT 1 FROM summary_reviews v WHERE v.entry_id=e.id)) ORDER BY s.created_at DESC,s.rowid DESC,e.rowid DESC",(int(active_only),int(active_only))).fetchall()
        picked=set();covered=set();records=[];snapshots={};currents={};rejections={};material_versions={}
        for row in rows:
            data=json.loads(row['data']);ids=json.loads(row['entry_ids'])
            original=json.loads(row['original'])
            key=(row['run_id'],*record_key(original))
            if active_only and original['kind']=='material' and any(material_versions.get((row['run_id'],i)) not in {None,row['summary_id']} for i in ids):
                covered.update(ids);continue
            if key in picked:continue
            picked.add(key)
            if row['summary_id'] not in snapshots:
                snapshots[row['summary_id']]={e['id']:e['data'] for e in json.loads(db.execute('SELECT inputs FROM knowledge_summaries WHERE id=?',(row['summary_id'],)).fetchone()[0])}
            if row['run_id'] not in currents:
                currents[row['run_id']]={r['id']:{**dict(r),'data':json.loads(r['data']),'source':json.loads(r['source'])} for r in db.execute('SELECT e.id,e.data,e.source,e.status,b.status AS batch_status FROM entries e LEFT JOIN batches b ON b.run_id=e.run_id AND b.position=e.position WHERE e.run_id=?',(row['run_id'],))}
            current=currents[row['run_id']];before=snapshots[row['summary_id']]
            if row['status']=='accepted' and any(not current.get(i) or current[i]['data']!=before.get(i) for i in ids):continue
            covered.update(ids)
            if original['kind']=='material':
                for i in ids:material_versions[(row['run_id'],i)]=row['summary_id']
            if row['status']!='accepted' or reference_entry(data):continue
            if not ids or any(not current.get(i) or current[i]['batch_status']!='done' for i in ids):continue
            if all(reference_entry(current[i]['data'],current[i]['source']) for i in ids):continue
            # Raw source rejections after a summary review take effect on future versions.
            if row['run_id'] not in rejections:
                rejections[row['run_id']]={r[0] for r in db.execute("SELECT id FROM entries WHERE run_id=? AND status='rejected'",(row['run_id'],))}
            rejected=rejections[row['run_id']]
            if rejected.intersection(ids):continue
            ownership=json.loads(row['ownership']);snapshot=json.loads(row['config']).get('ownership',{})
            company=db.execute("SELECT name FROM companies WHERE id='default'").fetchone()[0]
            owned=bool(ownership.get('confirmed') and ownership.get('company_name')==company and snapshot.get('confirmed') and snapshot.get('company_name')==company)
            sources=json.loads(row['sources'])
            records.append({'data':data,'sources':sources,'entry_ids':ids,'summary_entry_id':row['id'],'summary_id':row['summary_id'],'run_id':row['run_id'],'owned':owned})
        return records,covered
