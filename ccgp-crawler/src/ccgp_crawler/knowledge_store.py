from __future__ import annotations
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager, closing
from pathlib import Path
from .bid_models import BidCheckError
from .bid_store import now
from .knowledge_summary_store import SummaryStoreMixin
from .knowledge_views import KnowledgeViewMixin,reference_entry,note_view


def encoded(value): return json.dumps(value,ensure_ascii=False)
def uid(): return uuid.uuid4().hex

class KnowledgeStorageError(Exception):
    pass

class KnowledgeStore(KnowledgeViewMixin,SummaryStoreMixin):
    def __init__(self,runtime):
        self.root=Path(runtime)/'knowledge';self.path=self.root/'knowledge.sqlite3'
    @contextmanager
    def connect(self):
        try:
            self.root.mkdir(parents=True,exist_ok=True,mode=0o700);os.chmod(self.root,0o700)
            with closing(sqlite3.connect(self.path,timeout=10)) as db:
                os.chmod(self.path,0o600);db.row_factory=sqlite3.Row
                db.execute('PRAGMA foreign_keys=ON')
                with db: yield db
        except (sqlite3.Error, OSError):
            raise KnowledgeStorageError('企业知识库存储暂时不可用，请重试') from None
    def initialize(self):
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS companies(id TEXT PRIMARY KEY,name TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY,company_id TEXT NOT NULL, name TEXT NOT NULL,hash TEXT NOT NULL UNIQUE,size INTEGER NOT NULL,path TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,document_id TEXT NOT NULL REFERENCES documents(id),generation INTEGER NOT NULL,status TEXT NOT NULL,pages INTEGER NOT NULL DEFAULT 0,config TEXT,stop_requested INTEGER NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '',diagnostic TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS batches(run_id TEXT NOT NULL REFERENCES runs(id),position INTEGER NOT NULL,units TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',limitations TEXT NOT NULL DEFAULT '[]',error TEXT NOT NULL DEFAULT '',PRIMARY KEY(run_id,position));
            CREATE TABLE IF NOT EXISTS entries(id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES runs(id),position INTEGER NOT NULL,data TEXT NOT NULL,original TEXT NOT NULL,source TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS review_events(id INTEGER PRIMARY KEY,entry_id TEXT NOT NULL,action TEXT NOT NULL,before_data TEXT NOT NULL,after_data TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS revisions(id TEXT PRIMARY KEY,company_id TEXT NOT NULL,snapshot TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_k_runs_status ON runs(status,created_at);
            CREATE INDEX IF NOT EXISTS idx_k_entries_run ON entries(run_id,status);
            ''')
            batch_columns={row[1] for row in db.execute('PRAGMA table_info(batches)')}
            if 'ocr_prepared' not in batch_columns:db.execute('ALTER TABLE batches ADD COLUMN ocr_prepared INTEGER NOT NULL DEFAULT 0')
            db.execute('''CREATE TABLE IF NOT EXISTS ocr_results(run_id TEXT NOT NULL REFERENCES runs(id),source_id TEXT NOT NULL,location TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',job_id TEXT NOT NULL DEFAULT '',text TEXT NOT NULL DEFAULT '',scores TEXT NOT NULL DEFAULT '[]',error TEXT NOT NULL DEFAULT '',updated_at TEXT NOT NULL,PRIMARY KEY(run_id,source_id))''')
            columns={row[1] for row in db.execute('PRAGMA table_info(documents)')}
            if 'adopted_run_id' not in columns:
                db.execute('ALTER TABLE documents ADD COLUMN adopted_run_id TEXT')
                db.execute("UPDATE documents SET adopted_run_id=(SELECT r.id FROM runs r WHERE r.document_id=documents.id AND r.status IN ('awaiting_review','reviewed') AND EXISTS (SELECT 1 FROM entries e WHERE e.run_id=r.id AND e.status='accepted') ORDER BY r.generation DESC LIMIT 1)")
            db.execute('INSERT OR IGNORE INTO companies VALUES (?,?,?)',('default','',now()))
            self.initialize_summaries(db)
            self.refresh_reference_revision(db)
    def company(self):
        with self.connect() as db:
            row=dict(db.execute("SELECT * FROM companies WHERE id='default'").fetchone())
            revision=db.execute("SELECT id,snapshot,created_at FROM revisions ORDER BY rowid DESC LIMIT 1").fetchone()
            row['revision']= {'id':revision['id'],'created_at':revision['created_at'],**json.loads(revision['snapshot'])} if revision else None
            if row['revision']:
                for record in row['revision'].get('records',[]):record.update(note_view(record['data'].get('notes','')))
            row['document_count']=db.execute('SELECT count(*) FROM documents').fetchone()[0]
            return row
    def save_company(self,name):
        name=name.strip()
        if not name: raise BidCheckError('请填写本企业的完整名称')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE companies SET name=?,updated_at=? WHERE id='default'",(name,now()))
            self._revision(db)
        return self.company()
    def find_hash(self,digest):
        with self.connect() as db:
            row=db.execute('SELECT id FROM documents WHERE hash=?',(digest,)).fetchone()
        return row[0] if row else None
    def add_document(self,document_id,name,digest,size,path):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing=db.execute('SELECT id FROM documents WHERE hash=?',(digest,)).fetchone()
            if existing:return existing[0],False
            db.execute('INSERT INTO documents(id,company_id,name,hash,size,path,created_at) VALUES (?,?,?,?,?,?,?)',(document_id,'default',name,digest,size,str(path),now()))
            run=uid();db.execute('INSERT INTO runs(id,document_id,generation,status,created_at,updated_at) VALUES (?,?,1,?,?,?)',(run,document_id,'indexing',now(),now()))
        return document_id,True
    def document(self,document_id):
        with self.connect() as db:
            row=db.execute('SELECT * FROM documents WHERE id=?',(document_id,)).fetchone()
            if not row:raise KeyError(document_id)
            result=dict(row)
            result['ownership']=json.loads(result.get('ownership') or '{}')
            runs=db.execute('SELECT * FROM runs WHERE document_id=? ORDER BY generation DESC',(document_id,)).fetchall()
            result['runs']=[self._run_detail(db,r) for r in runs]
            result['run']=result['runs'][0]
            latest=db.execute('SELECT s.id FROM knowledge_summaries s JOIN runs r ON r.id=s.run_id WHERE r.document_id=? ORDER BY s.created_at DESC,s.rowid DESC LIMIT 1',(document_id,)).fetchone()
            result['summary']=self.summary_detail(latest[0]) if latest else None
            result.pop('path');result.pop('hash')
            return result
    def documents(self):
        with self.connect() as db:ids=[r[0] for r in db.execute('SELECT id FROM documents ORDER BY created_at DESC,rowid DESC')]
        return [self.document(i) for i in ids]
    def _run_detail(self,db,row):
        run=dict(row);run['config']=json.loads(run['config']) if run['config'] else None;run['diagnostic']=json.loads(run['diagnostic'])
        if run['config'] and run['config'].get('knowledge_skill'):
            run['config']['knowledge_skill']={k:v for k,v in run['config']['knowledge_skill'].items() if k!='instructions'}
        counts={r[0]:r[1] for r in db.execute('SELECT status,count(*) FROM batches WHERE run_id=? GROUP BY status',(run['id'],))}
        run.update(total_batches=sum(counts.values()),completed_batches=counts.get('done',0),failed_batches=counts.get('failed',0),pending_entries=self._pending_entries(db,run['id']))
        run['limitations']=[v for r in db.execute('SELECT limitations FROM batches WHERE run_id=?',(run['id'],)) for v in json.loads(r[0])]
        run['ocr']=self._ocr_summary(db,run['id'])
        recognized={r[0] for r in db.execute("SELECT source_id FROM ocr_results WHERE run_id=? AND status IN ('done','empty','failed')",(run['id'],))}
        run['pending_images']=sum(bool(u.get('visual')) and u['source_id'] not in recognized for row in db.execute("SELECT units FROM batches WHERE run_id=? AND status!='done' AND ocr_prepared=0",(run['id'],)) for u in json.loads(row[0]))
        return run
    def raw_run(self,run_id):
        with self.connect() as db:
            row=db.execute('SELECT r.*,d.path,d.name,d.company_id FROM runs r JOIN documents d ON d.id=r.document_id WHERE r.id=?',(run_id,)).fetchone()
            if not row:raise KeyError(run_id)
            result=dict(row);result['config']=json.loads(result['config']) if result['config'] else None
            return result
    def update_run(self,run_id,**fields):
        if not fields or not fields.keys()<={'status','pages','config','error','diagnostic','stop_requested'}:raise ValueError('invalid fields')
        fields['updated_at']=now()
        with self.connect() as db:db.execute('UPDATE runs SET '+','.join(f'{k}=?' for k in fields)+' WHERE id=?',(*fields.values(),run_id))
    def work(self):
        with self.connect() as db:
            row=db.execute("SELECT id FROM runs WHERE status IN ('indexing','queued') ORDER BY created_at LIMIT 1").fetchone()
            return row[0] if row else None
    def claim(self,run_id,status):
        target='indexing_running' if status=='indexing' else 'running'
        with self.connect() as db:return db.execute('UPDATE runs SET status=?,updated_at=? WHERE id=? AND status=? AND stop_requested=0',(target,now(),run_id,status)).rowcount==1
    def set_batches(self,run_id,pages,batches):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM batches WHERE run_id=?',(run_id,))
            db.executemany('INSERT INTO batches(run_id,position,units) VALUES (?,?,?)',((run_id,i,encoded(batch)) for i,batch in enumerate(batches)))
            db.execute("UPDATE runs SET pages=?,status=CASE WHEN stop_requested=1 THEN 'stopped' ELSE 'uploaded' END,updated_at=? WHERE id=?",(pages,now(),run_id))
    def _ocr_summary(self,db,run_id):
        counts={r[0]:r[1] for r in db.execute('SELECT status,count(*) FROM ocr_results WHERE run_id=? GROUP BY status',(run_id,))}
        issues=[dict(r) for r in db.execute("SELECT source_id,location,status,error FROM ocr_results WHERE run_id=? AND status IN ('empty','failed') ORDER BY rowid",(run_id,))]
        return {'total':sum(counts.values()),'processed':sum(counts.get(k,0) for k in ('done','empty','failed')),'failed':counts.get('empty',0)+counts.get('failed',0),'issues':issues}
    def ocr_summary(self,run_id):
        with self.connect() as db:return self._ocr_summary(db,run_id)
    def initialize_ocr(self,run_id,units):
        with self.connect() as db:db.executemany('INSERT OR IGNORE INTO ocr_results(run_id,source_id,location,updated_at) VALUES (?,?,?,?)',((run_id,u['source_id'],u['location'],now()) for u in units if u.get('visual')))
    def ocr_result(self,run_id,unit):
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO ocr_results(run_id,source_id,location,updated_at) VALUES (?,?,?,?)',(run_id,unit['source_id'],unit['location'],now()))
            row=dict(db.execute('SELECT * FROM ocr_results WHERE run_id=? AND source_id=?',(run_id,unit['source_id'])).fetchone())
            row['scores']=json.loads(row['scores']);return row
    def save_ocr(self,run_id,source_id,**fields):
        if not fields.keys()<={'status','job_id','text','scores','error'}:raise ValueError('invalid OCR fields')
        if 'scores' in fields:fields['scores']=encoded(fields['scores'])
        fields['updated_at']=now()
        with self.connect() as db:db.execute('UPDATE ocr_results SET '+','.join(k+'=?' for k in fields)+' WHERE run_id=? AND source_id=?',(*fields.values(),run_id,source_id))
    def unprepared_batches(self,run_id):
        with self.connect() as db:rows=db.execute("SELECT * FROM batches WHERE run_id=? AND status!='done' AND ocr_prepared=0 ORDER BY position",(run_id,)).fetchall()
        return [{**dict(r),'units':json.loads(r['units'])} for r in rows]
    def prepare_batch(self,run_id,position,batches,limitations):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT status,ocr_prepared FROM batches WHERE run_id=? AND position=?',(run_id,position)).fetchone()
            if not row or row['status']=='done' or row['ocr_prepared']:return
            extra=len(batches)-1
            if extra:
                # Shift later batches and their evidence atomically, retaining document order.
                for table in ('batches','entries'):
                    db.execute(f'UPDATE {table} SET position=-position-1 WHERE run_id=? AND position>?',(run_id,position))
                    db.execute(f'UPDATE {table} SET position=-position-1+? WHERE run_id=? AND position<0',(extra,run_id))
            db.execute('UPDATE batches SET units=?,limitations=?,ocr_prepared=1 WHERE run_id=? AND position=?',(encoded(batches[0]),encoded(limitations),run_id,position))
            for i,batch in enumerate(batches[1:],1):db.execute('INSERT INTO batches(run_id,position,units,ocr_prepared) VALUES (?,?,?,1)',(run_id,position+i,encoded(batch)))
    def next_batch(self,run_id):
        with self.connect() as db:row=db.execute("SELECT * FROM batches WHERE run_id=? AND status='pending' ORDER BY position LIMIT 1",(run_id,)).fetchone()
        return {**dict(row),'units':json.loads(row['units'])} if row else None
    def batch_state(self,run_id,position,status,error=''):
        with self.connect() as db:db.execute('UPDATE batches SET status=?,error=? WHERE run_id=? AND position=?',(status,error,run_id,position))
    def finish_run(self,run_id):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            remaining=db.execute("SELECT count(*) FROM batches WHERE run_id=? AND status!='done'",(run_id,)).fetchone()[0]
            pending=self._pending_entries(db,run_id)
            reviewed=db.execute("SELECT 1 FROM entries WHERE run_id=? AND status!='pending' LIMIT 1",(run_id,)).fetchone()
            status='failed' if remaining else 'awaiting_review' if pending or not reviewed else 'reviewed'
            diagnostic=json.loads(db.execute('SELECT diagnostic FROM runs WHERE id=?',(run_id,)).fetchone()[0])
            diagnostic['phase']='extraction_complete'
            db.execute('UPDATE runs SET status=?,error=?,diagnostic=?,updated_at=? WHERE id=?',(status,'部分批次未完成，请重试失败批次' if remaining else '',encoded(diagnostic),now(),run_id))
            if db.execute('SELECT 1 FROM documents WHERE adopted_run_id=?',(run_id,)).fetchone():self._revision(db)
    def finish_batch(self,run_id,position,output,units):
        source_map={u['source_id']:u for u in units}
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            batch=db.execute('SELECT status FROM batches WHERE run_id=? AND position=?',(run_id,position)).fetchone()
            if not batch or batch['status']=='done':return
            # Atomic batch commit means a recovered batch never produces duplicate entries.
            db.execute('DELETE FROM entries WHERE run_id=? AND position=?',(run_id,position))
            for entry in output.entries:
                data=entry.model_dump();unit=source_map[entry.source_id]
                segments=unit.get('segments',[])
                from .bid_models import contains_quote
                matched=[segment['location'] for segment in segments if contains_quote(segment['text'],entry.quote)]
                if not matched and segments:
                    # A quote crossing paragraph boundaries retains the contributing anchors.
                    compact=lambda value:''.join(value.split())
                    quote=compact(entry.quote);start=compact(unit['text']).find(quote)
                    if start>=0:
                        offset=0
                        for segment in segments:
                            length=len(compact(segment['text']))
                            if offset<start+len(quote) and offset+length>start:matched.append(segment['location'])
                            offset+=length
                from .knowledge_models import reference_only
                source={'source_id':unit['source_id'],'location':'；'.join(matched) if matched else unit['location'],'quote':entry.quote,'visual':bool(unit.get('visual')),'ocr':bool(unit.get('ocr')),'ocr_text':unit.get('ocr_text',''),'reference_only':reference_only(data)}
                db.execute('INSERT INTO entries(id,run_id,position,data,original,source,updated_at) VALUES (?,?,?,?,?,?,?)',(uid(),run_id,position,encoded(data),encoded(data),encoded(source),now()))
            previous=json.loads(db.execute('SELECT limitations FROM batches WHERE run_id=? AND position=?',(run_id,position)).fetchone()[0])
            db.execute("UPDATE batches SET status='done',error='',limitations=? WHERE run_id=? AND position=?",(encoded(list(dict.fromkeys(previous+output.limitations))),run_id,position))
    def action(self,document_id,action,config=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM runs WHERE document_id=? ORDER BY generation DESC LIMIT 1',(document_id,)).fetchone()
            if not row:raise KeyError(document_id)
            status=row['status'];run_id=row['id']
            if action=='stop':
                if status in {'awaiting_review','reviewed'}:raise BidCheckError('文档已完整拆解，无需停止')
                state='stopping' if status in {'running','indexing_running'} else 'stopped'
                db.execute('UPDATE runs SET stop_requested=1,status=?,updated_at=? WHERE id=?',(state,now(),run_id))
            elif action=='reextract':
                if status in {'queued','running','indexing','indexing_running','stopping'}:raise BidCheckError('请先停止当前拆解任务')
                run_id=uid();db.execute('INSERT INTO runs(id,document_id,generation,status,created_at,updated_at) VALUES (?,?,?,?,?,?)',(run_id,document_id,row['generation']+1,'indexing',now(),now()))
            elif action in {'start','retry'}:
                if status not in {'uploaded','stopped','failed'}:raise BidCheckError('当前文档状态不可开始或重试')
                count=db.execute('SELECT count(*) FROM batches WHERE run_id=?',(run_id,)).fetchone()[0]
                if count:
                    if not config:raise BidCheckError('请先配置模型')
                    db.execute("UPDATE batches SET status='pending',error='' WHERE run_id=? AND status IN ('failed','running')",(run_id,))
                target='queued' if count else 'indexing'
                db.execute('UPDATE runs SET status=?,config=?,stop_requested=0,error=\'\',updated_at=? WHERE id=?',(target,encoded(config),now(),run_id))
            else:raise BidCheckError('不支持此文档操作')
        return self.document(document_id)
    def recover(self):
        with self.connect() as db:
            db.execute("UPDATE batches SET status='pending' WHERE status='running'")
            db.execute("UPDATE runs SET status='queued' WHERE status='running' AND stop_requested=0")
            db.execute("UPDATE runs SET status='indexing' WHERE status='indexing_running' AND stop_requested=0")
            db.execute("UPDATE runs SET status='stopped' WHERE stop_requested=1 AND status='stopping'")
            db.execute("UPDATE knowledge_summaries SET status=CASE WHEN stop_requested=1 THEN 'stopped' ELSE 'queued' END WHERE status IN ('running','stopping')")
    def review(self,updates):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for update in updates:
                row=db.execute('SELECT e.*,r.status AS run_status,r.document_id,r.generation,b.status AS batch_status FROM entries e JOIN runs r ON e.run_id=r.id LEFT JOIN batches b ON b.run_id=e.run_id AND b.position=e.position WHERE e.id=?',(update['id'],)).fetchone()
                if not row:raise KeyError(update['id'])
                if row['batch_status']!='done':raise BidCheckError('本条所在批次尚未成功完成，暂时不能审核入库；请先重试该批次')
                status=update['status'];data=json.loads(row['data'])
                if reference_entry(data,json.loads(row['source'])):
                    raise BidCheckError('目录或空白模板仅作定位线索，请确认正文材料')
                for key,value in update.get('changes',{}).items():
                    if key in {'name','value','category','subject','applicable_date','industry','notes','needs_review'}:data[key]=value
                from .knowledge_models import ExtractedEntry
                data=ExtractedEntry.model_validate(data).model_dump()
                # Occurrence/evidence/source classifications remain immutable to preserve provenance.
                db.execute('UPDATE entries SET status=?,data=?,updated_at=? WHERE id=?',(status,encoded(data),now(),row['id']))
                db.execute('INSERT INTO review_events(entry_id,action,before_data,after_data,created_at) VALUES (?,?,?,?,?)',(row['id'],status,encoded({'status':row['status'],'data':json.loads(row['data'])}),encoded(data),now()))
                pending=self._pending_entries(db,row['run_id'])
                # Reviewing completed batches must not stop a worker or hide failed batches.
                complete=row['run_status'] in {'awaiting_review','reviewed'}
                if complete:
                    db.execute('UPDATE runs SET status=? WHERE id=?',('awaiting_review' if pending else 'reviewed',row['run_id']))
                # Explicit adoption prevents withdrawn newer results resurrecting older facts.
                if status=='accepted' or (complete and not pending):
                    db.execute('UPDATE documents SET adopted_run_id=? WHERE id=? AND COALESCE((SELECT generation FROM runs WHERE id=adopted_run_id),0)<=?',(row['run_id'],row['document_id'],row['generation']))
            revision=self._revision(db)
        return {'revision_id':revision}
    def active(self,document_id,active):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('UPDATE documents SET active=? WHERE id=?',(int(active),document_id)).rowcount:raise KeyError(document_id)
            self._revision(db)
        return self.document(document_id)
    def _revision(self,db):
        company=db.execute("SELECT name FROM companies WHERE id='default'").fetchone()[0]
        rows=db.execute("SELECT e.*,r.document_id,d.name AS filename,d.ownership FROM entries e JOIN runs r ON r.id=e.run_id JOIN documents d ON d.id=r.document_id JOIN batches b ON b.run_id=e.run_id AND b.position=e.position WHERE e.status='accepted' AND d.active=1 AND r.id=d.adopted_run_id AND b.status='done'").fetchall()
        summary_records,covered=self._summary_revision_records(db)
        grouped={};limitations=[]
        for row in rows:
            if row['id'] in covered:continue
            data=json.loads(row['data']);source=json.loads(row['source'])
            if reference_entry(data,source):continue
            from .knowledge_models import local_absence
            if local_absence(data['value'],source['quote']):continue
            key=encoded([data[k] for k in ('kind','category','name','value','subject','occurrence','evidence_kind','applicable_date','industry','needs_review','notes')])
            ownership=json.loads(row['ownership'])
            owned=bool(ownership.get('confirmed') and ownership.get('company_name')==company)
            group=grouped.setdefault(key,{'data':data,'sources':[],'entry_ids':[],'owned':owned})
            group['owned']=group['owned'] and owned
            group['sources'].append({'document_id':row['document_id'],'filename':row['filename'],**source});group['entry_ids'].append(row['id'])
        records=list(grouped.values())+summary_records;facts=[];sources=[];unsafe=[];materials=[]
        conflicts={}
        for group in records:
            d=group['data'];k=(d['category'],d['name'],d['subject']);conflicts.setdefault(k,set()).add(d['value'])
        for i,group in enumerate(records):
            d=group['data'];fid=f'K{i+1}';first=group['sources'][0]
            # Historical time-sensitive statements cannot establish current noncompliance.
            historical = d['category'] in {'证照资质','资格声明','企业规模'} and bool(d['applicable_date'])
            basic_support=bool(not any(word in d['value'] for word in ('不具备','不具有','吊销','撤销')) and group.get('owned') and d['occurrence']=='actual' and d.get('subject_role')=='supplier' and d['evidence_kind'] in {'proof','declaration'})
            group['basic_support']=basic_support
            uncertain=d['needs_review'] or historical or any(source.get('ocr') for source in group['sources']) or d['evidence_kind'] in {'inference','unknown'} or d['occurrence']!='actual' or d['subject']!=company or d.get('subject_role') in {'manufacturer','customer','third_party'} or len(conflicts[(d['category'],d['name'],d['subject'])])>1 or d['kind']=='material'
            if basic_support:uncertain=True  # historical/OCR materials support positive screening, never definite exclusion
            if uncertain:unsafe.append(fid)
            metadata=f"类型：{'材料名称' if d['kind']=='material' else '企业事实'}；主体：{d['subject'] or '未知'}；出现方式：{d['occurrence']}；依据：{d['evidence_kind']}；时间：{d['applicable_date'] or '未知'}；行业：{d['industry'] or '未注明'}"
            statement=f"{d['name']}：{d['value']}。{metadata}。{note_view(d['notes'])['current_notes']}"
            if basic_support:
                statement+='。本企业已确认历史资料中的供应商材料/声明，支持一般资格初筛；历史日期、OCR及编号不全不单独触发待核实，额外等级/范围/数量等仍核对。'
                materials.append({'fact_id':fid,'kind':d['kind'],'name':d['name'],'value':d['value'],'evidence_kind':d['evidence_kind'],'certificates':d.get('certificates',[]),'sources':group['sources']})
            facts.append({'id':fid,'statement':statement,'source_id':fid,'quote':first['quote']})
            sources.append({'source_id':fid,'kind':'text','text':first['quote'],'unsafe_for_conflict':uncertain,'provenance':group['sources']})
            group['conflict']=len(conflicts[(d['category'],d['name'],d['subject'])])>1
            if uncertain and not basic_support:limitations.append(f"{d['name']}：仅材料名称或信息需核实，不作为明确硬性冲突依据")
        # Cross-batch limitations are carried into the reusable profile.
        for row in db.execute("SELECT b.limitations FROM batches b JOIN runs r ON r.id=b.run_id JOIN documents d ON d.id=r.document_id WHERE d.active=1 AND r.id=d.adopted_run_id AND b.status='done'"):
            for value in json.loads(row[0]):
                if any(term in value for term in ('OCR 待核实','无法读取','文字不清','读取失败')):limitations.append('局部读取限制：'+value)
        for row in db.execute("SELECT d.name,count(*) AS total,sum(b.status='done') AS completed FROM documents d JOIN runs r ON r.id=d.adopted_run_id JOIN batches b ON b.run_id=r.id WHERE d.active=1 GROUP BY r.id HAVING completed<total"):
            limitations.append(f"{row['name']}：仅采用已确认的成功批次，文档尚未完整拆解（已完成 {row['completed']}/{row['total']} 批）；未完成内容不代表材料缺失或资格不符")
        run_ids={r[0] for r in db.execute('SELECT adopted_run_id FROM documents WHERE active=1 AND adopted_run_id IS NOT NULL')}
        coverage=[self._coverage(db,rid) for rid in sorted(run_ids)]
        for item in coverage:
            summary_ids={r['summary_id'] for r in summary_records if r['run_id']==item['run_id']}
            item['summary_gaps']=[gap for sid in sorted(summary_ids) for gap in self._summary_gaps(db,sid)]
            if item['summary_gaps']:
                limitations.append(f"{item['filename']}：部分材料汇总未完成，相关内容未知，不表示材料缺失或资格不符")
        snapshot={'company_name':company,'records':records,'material_count':sum(r['data']['kind']=='material' for r in records),'fact_count':sum(r['data']['kind']=='fact' for r in records),'profile':{'company_name':company,'facts':facts,'limitations':list(dict.fromkeys(limitations))},'sources':sources,'unsafe_fact_ids':unsafe,'material_support':materials,'coverage':coverage}
        revision=uid();db.execute('INSERT INTO revisions VALUES (?,?,?,?)',(revision,'default',encoded(snapshot),now()));return revision
    def revision(self,revision_id):
        with self.connect() as db:row=db.execute('SELECT snapshot FROM revisions WHERE id=?',(revision_id,)).fetchone()
        if not row:raise KeyError(revision_id)
        data=json.loads(row[0])
        if not data['profile']['facts']:raise BidCheckError('知识库尚无已确认资料，请先确认拆解结果')
        if len(encoded(data['profile']))>200_000:raise BidCheckError('已确认资料超过检标单次范围，请停用无关文档后重试；未截断资料')
        return data
    def original_path(self,document_id):
        with self.connect() as db:row=db.execute('SELECT path,name FROM documents WHERE id=?',(document_id,)).fetchone()
        if not row:raise KeyError(document_id)
        return Path(row['path']),row['name']
