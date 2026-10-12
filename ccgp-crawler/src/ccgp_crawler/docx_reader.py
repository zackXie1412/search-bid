"""Ordered OOXML evidence with structural anchors; never extract media to disk."""
from __future__ import annotations

import io
import warnings
from PIL import Image
import posixpath
import re
import zipfile
from collections import Counter
from pathlib import Path
from lxml import etree
from .bid_models import BidCheckError

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
MC = 'http://schemas.openxmlformats.org/markup-compatibility/2006'
V = 'urn:schemas-microsoft-com:vml'
BLOCK_CHARS = 4000


class NeedsOfficeRendering(BidCheckError):
    pass


def local(node):
    return etree.QName(node).localname if isinstance(node.tag, str) else ''


def selected(node):
    """Visit one compatibility branch, ignoring deleted revisions."""
    if local(node) in {'del','moveFrom'}: return
    yield node
    children = list(node)
    if node.tag == f'{{{MC}}}AlternateContent':
        choice = next((c for c in children if local(c)=='Choice'), None)
        fallback = next((c for c in children if local(c)=='Fallback'), None)
        # Prefer modern content with readable text/images; otherwise use the VML fallback.
        useful = choice is not None and any(local(e) in {'t','blip','relIds','txbxContent'} for e in choice.iter())
        children = [choice if useful else fallback] if useful or fallback is not None else [choice] if choice is not None else []
    for child in children:
        yield from selected(child)


class DocxReader:
    def __init__(self, path, *, progress=None, check=None):
        self.path = Path(path)
        self.progress = progress or (lambda **kw: None)
        self.check = check or (lambda: None)
        self.stats = Counter(paragraphs=0,tables=0,images=0,textboxes=0,smartart=0,text_characters=0)
        self.units = []
        self.pending = []
        self.pending_chars = 0
        self.media = set()
        self.unsupported = []
        self.paragraph_numbers = Counter()
        self.table_numbers = Counter()
        self.image_formats = {}
        self.parser = etree.XMLParser(resolve_entities=False,no_network=True)

    def xml(self, part):
        self.check()
        try: return etree.fromstring(self.archive.read(part),self.parser)
        except (KeyError,etree.XMLSyntaxError,zipfile.BadZipFile):
            raise BidCheckError(f'DOCX 部件无法读取：{part}；读取不完整') from None

    def relationships(self, part):
        name = posixpath.join(posixpath.dirname(part),'_rels',posixpath.basename(part)+'.rels')
        if name not in self.names:return {}
        result = {}
        for rel in self.xml(name):
            target = rel.get('Target','')
            resolved = target.lstrip('/') if target.startswith('/') else posixpath.normpath(posixpath.join(posixpath.dirname(part),target))
            result[rel.get('Id')] = {'target':resolved,'external':rel.get('TargetMode')=='External','type':rel.get('Type','').rsplit('/',1)[-1]}
        return result

    def flush(self):
        if not self.pending:return
        start = 0;segments=[];texts=[]
        for location,text in self.pending:
            texts.append(text)
            segments.append({'location':location,'start':start,'end':start+len(text),'text':text})
            start += len(text)+1
        location = segments[0]['location']
        if len(segments)>1:location += ' 至 '+segments[-1]['location']
        self.units.append({'location':location,'text':'\n'.join(texts),'visual':None,'segments':segments})
        self.pending=[];self.pending_chars=0

    def text(self, location, value):
        if not value.strip():return
        self.stats['text_characters']+=len(value)
        for offset in range(0,len(value),BLOCK_CHARS):
            part=value[offset:offset+BLOCK_CHARS]
            anchor=location+(f' · 字符{offset+1}—{offset+len(part)}' if len(value)>BLOCK_CHARS else '')
            if self.pending and self.pending_chars+len(part)+1>BLOCK_CHARS:self.flush()
            self.pending.append((anchor,part));self.pending_chars+=len(part)+1

    def image_format(self, rel):
        if not rel or rel['external'] or rel['target'] not in self.names:return None
        target=rel['target']
        if target not in self.image_formats:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore',Image.DecompressionBombWarning)
                    with Image.open(io.BytesIO(self.archive.read(target))) as image:
                        self.image_formats[target]=image.format
            except Exception:self.image_formats[target]=None
        return self.image_formats[target]

    def compatibility_branch(self, node, rels):
        choices=[c for c in node if local(c)=='Choice']
        fallback=next((c for c in node if local(c)=='Fallback'),None)
        for choice in choices:
            has_content=False;valid=True
            for n in choice.iter():
                tag=local(n)
                if tag in {'t','relIds','txbxContent'}:has_content=True
                if tag in {'blip','imagedata'}:
                    rid=n.get(f'{{{R}}}embed') or n.get(f'{{{R}}}link') or n.get(f'{{{R}}}id')
                    if rid:
                        has_content=True
                        format=self.image_format(rels.get(rid))
                        if not format or format in {'WMF','EPS'}:valid=False
            if has_content and valid:return choice
        return fallback if fallback is not None else choices[0] if choices else None

    def image(self, location, rel, context):
        self.flush()
        if rel is None:
            raise BidCheckError(f'{location}：图片引用丢失；读取不完整')
        if rel['external']:
            raise BidCheckError(f'{location}：外部链接图片不在原件中，请嵌入图片后上传')
        target=rel['target']
        if target not in self.names:raise BidCheckError(f'{location}：嵌入图片不存在；读取不完整')
        if self.image_format(rel) in {'WMF','EPS'}:
            self.unsupported.append(location+' · 无法直接解码的矢量图片');return
        self.stats['images']+=1;self.media.add(target)
        self.units.append({'location':location+f' · 图片{self.stats["images"]}','text':context[-1000:],
            'visual':{'kind':'docx','path':str(self.path),'media':target},'segments':[]})

    def diagram(self, part, rel, location):
        if rel is None or rel['external']:raise BidCheckError(f'{location}：SmartArt 引用无法读取')
        target=rel['target']
        # Only referenced diagram data; never read both drawing and data copies.
        self.stats['smartart']+=1
        root=self.xml(target);rels=self.relationships(target)
        self.flush()
        for index,point in enumerate((n for n in selected(root) if local(n)=='pt'),1):
            self.inline(point,target,rels,f'{location} · SmartArt节点{index}')
        # Diagram drawing contains layout copies of node text; read only its images.
        for related in rels.values():
            if related['type']=='diagramDrawing' and not related['external']:
                drawing=self.xml(related['target']);drawing_rels=self.relationships(related['target'])
                for image in selected(drawing):
                    if local(image)=='blip':
                        rid=image.get(f'{{{R}}}embed') or image.get(f'{{{R}}}link')
                        if rid:self.image(location+' · SmartArt关联图片',drawing_rels.get(rid),'')
        self.flush()

    def inline(self, root, part, rels, location):
        text=[]
        context='\n'.join(t for _,t in self.pending)[-1000:]
        def flush_text():
            nonlocal context
            value=''.join(text)
            if value:
                self.text(location,value);context=(context+'\n'+value)[-1000:]
            text.clear()
        def visit(node):
            tag=local(node)
            namespace=etree.QName(node).namespace if isinstance(node.tag,str) else ''
            if tag in {'del','moveFrom'}:return
            if node.tag==f'{{{MC}}}AlternateContent':
                branch=self.compatibility_branch(node,rels)
                if branch is not None:visit(branch)
                return
            if tag=='t' and namespace in {W,A}:text.append(node.text or '');return
            if tag=='tab' and namespace==W:text.append('\t');return
            if tag in {'br','cr'} and namespace==W:text.append('\n');return
            if tag=='txbxContent':
                flush_text();self.flush();self.stats['textboxes']+=1
                self.walk(node,part,rels,location+' · 文本框'+str(self.stats['textboxes'])+' · ')
                self.flush();return
            if tag in {'blip','imagedata'}:
                flush_text()
                rid=node.get(f'{{{R}}}embed') or node.get(f'{{{R}}}link') or node.get(f'{{{R}}}id') or node.get(f'{{{R}}}href')
                if rid:self.image(location,rels.get(rid),context)
                elif tag=='imagedata' and not node.get('src'):
                    # Word emits empty VML image placeholders alongside readable shapes.
                    self.stats['empty_image_placeholders']+=1
                else:self.unsupported.append(location+' · 无法直接读取的图像')
                return
            if tag=='relIds':
                flush_text();self.diagram(part,rels.get(node.get(f'{{{R}}}dm')),location);return
            if tag in {'chart','OLEObject','altChunk','object'}:
                self.unsupported.append(location+' · '+tag)
            if tag=='graphicData':
                uri=node.get('uri','')
                if uri and not any(key in uri for key in ('picture','diagram','wordprocessingShape','wordprocessingGroup')):
                    self.unsupported.append(location+' · 图形')
            if tag in {'footnoteReference','endnoteReference'}:
                flush_text();self.flush()
                kind='footnote' if tag=='footnoteReference' else 'endnote'
                note_part=f'word/{kind}s.xml'
                if note_part not in self.names:raise BidCheckError(f'{location}：注释部件缺失')
                identifier=node.get(f'{{{W}}}id')
                note=next((n for n in self.xml(note_part) if n.get(f'{{{W}}}id')==identifier),None)
                if note is None:raise BidCheckError(f'{location}：注释引用缺失')
                self.walk(note,note_part,self.relationships(note_part),f'{location} · {"脚注" if kind=="footnote" else "尾注"}{identifier} · ')
                self.flush();return
            for child in node:visit(child)
        visit(root);flush_text()

    def walk(self, root, part, rels, prefix):
        for child in root:
            self.check()
            tag=local(child)
            if tag=='p':
                self.paragraph_numbers[part]+=1;self.stats['paragraphs']+=1
                anchor=f'{prefix}第{self.paragraph_numbers[part]}段'
                heading=child.find(f'.//{{{W}}}pStyle')
                is_heading=heading is not None and re.search(r'heading|标题',heading.get(f'{{{W}}}val',''),re.I)
                if is_heading:self.flush()
                self.inline(child,part,rels,anchor)
                if is_heading:self.flush()
            elif tag=='tbl':
                self.flush();self.stats['tables']+=1;self.table_numbers[part]+=1
                table=f'{prefix}第{self.table_numbers[part]}个表格'
                for row_no,row in enumerate(child.findall(f'{{{W}}}tr'),1):
                    before=row.find(f'{{{W}}}trPr/{{{W}}}gridBefore')
                    col=1+int(before.get(f'{{{W}}}val','0')) if before is not None else 1
                    for cell in row.findall(f'{{{W}}}tc'):
                        self.check()
                        props=cell.find(f'{{{W}}}tcPr')
                        span_node=props.find(f'{{{W}}}gridSpan') if props is not None else None
                        span=int(span_node.get(f'{{{W}}}val','1')) if span_node is not None else 1
                        merge=props.find(f'{{{W}}}vMerge') if props is not None else None
                        qualifier=f'（合并{span}列）' if span>1 else ''
                        if merge is not None:qualifier+='（纵向合并）'
                        cell_anchor=f'{table}第{row_no}行第{col}列{qualifier}'
                        self.walk(cell,part,rels,cell_anchor+' · ')
                        col+=span
                self.flush()
            elif tag in {'sdt','sdtContent','ins','moveTo','customXml'}:
                self.walk(child,part,rels,prefix)
            elif tag=='AlternateContent':
                branch=self.compatibility_branch(child,rels)
                if branch is not None:
                    if any(local(n) in {'p','tbl'} for n in branch):self.walk(branch,part,rels,prefix+'兼容内容 · ')
                    else:self.inline(branch,part,rels,prefix+'兼容内容')
            elif tag=='altChunk':self.unsupported.append(prefix+' · 外部嵌入正文')
            elif tag not in {'sectPr','tcPr','sdtPr','sdtEndPr'} and any(local(n) in {'t','blip','imagedata','chart','object'} for n in child.iter()):
                self.unsupported.append(prefix+' · 未支持的结构 '+tag)
            if tag=='p' and self.stats['paragraphs']%100==0:
                self.progress(phase='docx_read',processed=self.stats['paragraphs'],stats=dict(self.stats))

    def read(self):
        self.progress(phase='docx_read',processed=0)
        try:
            with zipfile.ZipFile(self.path) as self.archive:
                self.names=set(self.archive.namelist())
                if 'word/document.xml' not in self.names:raise BidCheckError('DOCX 缺少正文，原文件可能损坏')
                root=self.xml('word/document.xml');rels=self.relationships('word/document.xml')
                body=root.find(f'{{{W}}}body')
                if body is None:raise BidCheckError('DOCX 缺少正文结构')
                self.walk(body,'word/document.xml',rels,'正文');self.flush()
                # Read only attached headers/footers, once per part.
                referenced=[]
                for node in selected(root):
                    if local(node) in {'headerReference','footerReference'}:
                        rel=rels.get(node.get(f'{{{R}}}id'))
                        if rel is None or rel['external']:raise BidCheckError('DOCX 页眉页脚引用无法读取')
                        if rel['target'] not in referenced:referenced.append(rel['target'])
                for part in referenced:
                    prefix=('页眉' if 'header' in part else '页脚')+' '+Path(part).stem+' · '
                    self.walk(self.xml(part),part,self.relationships(part),prefix);self.flush()
                self.stats['unique_images']=len(self.media)
                self.stats['unreferenced_media']=sum(n.startswith('word/media/') and not n.endswith('/') and n not in self.media for n in self.names)
                try:
                    metadata=self.xml('docProps/app.xml')
                    self.stats['reference_pages']=next((int(n.text) for n in metadata if local(n)=='Pages' and (n.text or '').isdigit()),0)
                except BidCheckError:self.stats['reference_pages']=0
        except (zipfile.BadZipFile,OSError):raise BidCheckError('DOCX 无法打开，原文件可能损坏') from None
        if self.unsupported:
            self.progress(phase='office_convert',unreadable=list(dict.fromkeys(self.unsupported)),stats=dict(self.stats))
            raise NeedsOfficeRendering('需要转换的内容：'+ '；'.join(dict.fromkeys(self.unsupported))[:500])
        for index,unit in enumerate(self.units,1):unit['source_id']=f'S{index}'
        self.stats['blocks']=len(self.units)
        return self.units, {'mode':'docx_direct','unit_label':'内容块','unreadable':[],**dict(self.stats)}
