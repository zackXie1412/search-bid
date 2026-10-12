"""Derived audit views; never rewrite extraction or human review history."""
import json
import re
from .knowledge_models import reference_only


def reference_entry(data, source=None):
    source=source or {}
    if source.get('reference_only') or reference_only(data):return True
    if data.get('occurrence')!='third_party':return False
    # Old name mismatch replaced occurrence. Recover only explicit clue descriptions,
    # never from a company name difference or a standalone absence observation.
    text=' '.join(str(data.get(k,'')) for k in ('name','value','notes'))
    quote=source.get('quote') or data.get('quote','')
    evidence=re.search(r'统一社会信用代码|许可证编号|注册号|证书编号|法定代表人|我方.{0,40}(?:声明|具备|承诺)|本(?:企业|公司).{0,40}(?:声明|具备|承诺)',quote)
    clue=re.search(r'仅.{0,6}目录|在目录中提及|目录(?:中)?(?:列有|列出|包含|提及)|空白(?:表格|模板|声明函)|仅.{0,6}表头',text)
    return bool(clue and not evidence)


def note_view(notes):
    """Separate local absence clauses, keeping independent uncertainty untouched."""
    local=[];current=[]
    for clause in re.split(r'[；;。\n，,]+|(?:但|并且|且|同时|不过)(?=[^；。\n，,]*(?:文字不清|材料类型|有效期|编号|信用|等级|范围))',notes or ''):
        clause=clause.strip()
        if not clause:continue
        observation=re.search(r'(?:本批|本页|本段|当前批次|本批次).{0,16}(?:未见|未找到|未发现|未出现|未提取|无正文)|未在正文(?:段落)?中(?:提取|识别)|(?:正文|扫描件|证件页|声明函).{0,8}(?:本批未见|本批未找到)|(?:仅有|仅在|在|仅)?目录.{0,12}(?:提及|列出)|仅.{0,8}(?:标题|表头|空白模板)|未(?:直接)?(?:提供|展示|出现).{0,18}(?:正文|扫描件)|未见(?:证明材料|声明函|承诺函|许可证|报告)正文',clause)
        if observation or (local and re.search(r'^(?:因此|故|所以|无法|不能|未能).*(?:确认|判断|认定|证明).*(?:实际|持有|存在|正文|材料)',clause) and not re.search(r'有效期|文字不清|编号|信用|等级|范围',clause)):local.append(clause)
        else:current.append(clause)
    return {'local_observations':local,'current_notes':'；'.join(current)}


class KnowledgeViewMixin:
    def _entry_views(self,db,document_id=None,run_id=None):
        cond=[];values=[]
        if document_id:cond.append('r.document_id=?');values.append(document_id)
        if run_id:cond.append('r.id=?');values.append(run_id)
        rows=db.execute('SELECT e.*,r.document_id,r.generation,r.status AS run_status,b.status AS batch_status,d.name AS filename,d.active FROM entries e JOIN runs r ON r.id=e.run_id JOIN documents d ON d.id=r.document_id LEFT JOIN batches b ON b.run_id=e.run_id AND b.position=e.position'+(' WHERE '+' AND '.join(cond) if cond else '')+' ORDER BY d.created_at DESC,d.rowid DESC,r.generation DESC,e.position,e.rowid',values).fetchall()
        records,_=self._summary_revision_records(db,active_only=False)
        links={}
        for record in records:
            d=record['data']
            link={'summary_id':record['summary_id'],'entry_id':record['summary_entry_id'],'name':d['name'],'kind':d['kind'],'occurrence':d['occurrence'],'value':d['value']}
            for id in record['entry_ids']:links.setdefault(id,[]).append(link)
        items=[]
        for row in rows:
            item=dict(row)
            for field in ('data','original','source'):item[field]=json.loads(item[field])
            related=links.get(item['id'],[])
            reference=reference_entry(item['data'],item['source'])
            item.update(reference_only=reference,summary_links=related,
                display_status='summary_confirmed' if related else 'reference' if reference else item['status'],
                effective_status='accepted' if related else item['status'],
                reviewable=item['batch_status']=='done' and not reference and not related)
            item.update(note_view(item['data'].get('notes','')))
            if reference:
                # Clue/provenance descriptions are historic context. Keep concrete
                # independent problems visible, including mixed legacy remarks.
                current=[]
                for clause in item['current_notes'].split('；'):
                    if not clause:continue
                    clue_observation=re.search(r'标题性|仅.{0,8}(?:标题|条款)|(?:目录|说明书).*(?:提及|出现)|非.*原件|未.*(?:提供|展示).*(?:本身|正文|扫描件)|需结合.*(?:来源|证件).*核|^OCR(?:文本| 识别文字)$|^需核对原件$',clause)
                    independent=re.search(r'不清|模糊|无法辨认|材料类型|有效期|编号|信用|等级|范围|主体|归属',clause)
                    if clue_observation and not independent:item['local_observations'].append(clause)
                    else:current.append(clause)
                item['current_notes']='；'.join(current)
            actual=[link for link in related if link['occurrence']=='actual' and link['kind']==item['data']['kind']]
            item['current_material_status']=(('已发现实际'+actual[0]['name']) if item['data']['kind']=='material' else '已确认能力摘要') if actual else '定位线索' if reference else ''
            items.append(item)
        return items

    def _pending_entries(self,db,run_id):
        return sum(item['status']=='pending' and not item['reference_only'] and not item['summary_links'] for item in self._entry_views(db,run_id=run_id))

    def entries(self,document_id=None,status=None,query='',category=None,page=1,view='review'):
        with self.connect() as db:entries=self._entry_views(db,document_id)
        items=[]
        for entry in entries:
            if view=='review' and (entry['reference_only'] or entry['summary_links']):continue
            if status and entry['effective_status']!=status:continue
            if category and entry['data']['category']!=category:continue
            if query and query.casefold() not in json.dumps(entry,ensure_ascii=False).casefold():continue
            items.append(entry)
        return {'items':items[(page-1)*30:page*30],'total':len(items),'page':page,'has_next':len(items)>page*30,'view':view}

    def _refresh_review_state(self,db,run_id):
        # Extraction execution/failure states are independent of evidence review.
        state=db.execute('SELECT status FROM runs WHERE id=?',(run_id,)).fetchone()[0]
        if state in {'awaiting_review','reviewed'}:
            db.execute('UPDATE runs SET status=? WHERE id=?',('awaiting_review' if self._pending_entries(db,run_id) else 'reviewed',run_id))

    def refresh_reference_revision(self,db):
        """Idempotent upgrade of future evidence, retaining every prior snapshot."""
        row=db.execute('SELECT snapshot FROM revisions ORDER BY rowid DESC LIMIT 1').fetchone()
        if not row:return
        current=json.loads(row[0]);stale=False
        for record in current.get('records',[]):
            if reference_entry(record['data']):stale=True;break
            for id in record.get('entry_ids',[]):
                source=db.execute('SELECT data,source FROM entries WHERE id=?',(id,)).fetchone()
                if source and reference_entry(json.loads(source['data']),json.loads(source['source'])):
                    # Actual summaries may legitimately cite a directory as auxiliary evidence.
                    if not record.get('summary_entry_id'):stale=True
            if record.get('summary_entry_id') and record.get('entry_ids'):
                originals=[db.execute('SELECT data,source FROM entries WHERE id=?',(i,)).fetchone() for i in record['entry_ids']]
                if all(v and reference_entry(json.loads(v['data']),json.loads(v['source'])) for v in originals):stale=True
            if stale:break
        if stale:self._revision(db)
