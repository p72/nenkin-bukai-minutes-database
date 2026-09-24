"""Build a searchable SQLite database from MHLW pension committee minutes."""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
import time
import unicodedata
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

from lxml import html

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent if HERE.name in ('work', 'outputs') else HERE
CACHE = ROOT / 'work' / 'source_cache'
OUT = ROOT / 'outputs'
INDEX_URL = 'https://www.mhlw.go.jp/stf/shingi/shingi-hosho_126721.html'
UA = 'Mozilla/5.0 (compatible; public-research/1.0)'


def fetch(url: str, refresh: bool = False) -> bytes:
    CACHE.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(url.encode()).hexdigest()
    path = CACHE / key
    if path.exists() and not refresh:
        return path.read_bytes()
    error = None
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={'User-Agent': UA})
            with urllib.request.urlopen(request, timeout=45) as response:
                data = response.read()
            path.write_bytes(data)
            return data
        except Exception as exc:
            error = exc
            time.sleep(1 + attempt * 2)
    raise RuntimeError(f'{url}: {error}')


def clean(s: str) -> str:
    return re.sub(r'\s+', ' ', s).strip()


def index_rows() -> list[dict]:
    tree = html.fromstring(fetch(INDEX_URL, refresh=True))
    result = []
    for index, tr in enumerate(tree.xpath('//table//tr')[1:], 1):
        cells = tr.xpath('./td')
        if len(cells) == 4 and result:
            # A row with rowspans shares its meeting label/date with the previous row.
            result[-1]['agenda'] += ' / ' + clean(cells[0].text_content())
            result[-1]['sources'].extend((clean(a.text_content()), urljoin(INDEX_URL, a.get('href') or '')) for a in cells[1].xpath('.//a'))
            continue
        if len(cells) < 6:
            continue
        date_match = re.search(r'(20[0-9]{2})年([0-9]{1,2})月([0-9]{1,2})日', unicodedata.normalize('NFKC', cells[1].text_content()))
        date = '-'.join((date_match.group(1), date_match.group(2).zfill(2), date_match.group(3).zfill(2))) if date_match else None
        links = [(clean(a.text_content()), urljoin(INDEX_URL, a.get('href') or '')) for a in cells[3].xpath('.//a')]
        result.append({
            'index_order': index,
            'meeting_label': clean(cells[0].text_content()),
            'date': date,
            'agenda': clean(cells[2].text_content()),
            'sources': links,
        })
    return result


def decode_txt(data: bytes) -> str:
    for encoding in ('utf-8-sig', 'cp932'):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            pass
    return data.decode('cp932', errors='replace')


def node_text(node) -> str:
    """Text with line boundaries preserved for both br-heavy and paragraph HTML."""
    parts = []
    blocks = {'p', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'li', 'tr', 'dt', 'dd', 'section'}
    def visit(el):
        tag = el.tag.lower() if isinstance(el.tag, str) else ''
        if tag in ('script', 'style', 'nav', 'footer'):
            return
        if tag in blocks:
            parts.append('\n')
        if el.text:
            parts.append(el.text)
        for child in el:
            if child.tag == 'br':
                parts.append('\n')
            else:
                visit(child)
            if child.tail:
                parts.append(child.tail)
        if tag in blocks:
            parts.append('\n')
    visit(node)
    s = ''.join(parts).replace('\r\n', '\n').replace('\r', '\n')
    return re.sub(r'\n[ \t\u3000]*\n+', '\n', s)


def html_text(data: bytes) -> str:
    tree = html.fromstring(data)
    # New templates put the full transcript in one dl/dd; old ones use #contents.
    dd = tree.xpath('//main//dl/dd')
    transcript_dd = [x for x in dd if len(x.text_content()) > 1000 and re.search(r'[○◯〇●].{1,25}(?:委員|部会長|課長)', x.text_content())]
    if transcript_dd:
        node = max(transcript_dd, key=lambda x: len(x.text_content()))
    else:
        content_columns = tree.xpath('//main//*[contains(concat(" ",normalize-space(@class)," ")," m-grid__col1 ")]')
        transcript_columns = [x for x in content_columns if len(x.text_content()) > 1000 and re.search(r'[○◯〇●].{1,25}(?:委員|部会長|課長)', x.text_content())]
        if transcript_columns:
            node = max(transcript_columns, key=lambda x: len(x.text_content()))
        else:
            contents = tree.xpath('//*[@id="contents"]')
            candidates = tree.xpath('//*[@id="main"] | //main')
            node = contents[0] if contents else (candidates[0] if candidates else tree)
    return node_text(node)


def source_text(url: str, data: bytes) -> str:
    return decode_txt(data) if urlparse(url).path.lower().endswith('.txt') else html_text(data)


# A speaker line begins with a meeting transcript marker, a short label and a separator.
# The preamble's headings (○日時, ○議題, etc.) are excluded.
SPEAKER_LINE = re.compile(r'^\s*[○◯〇●]\s*([^\s○◯〇●]{1,32}(?:[ \u3000][^\s○◯〇●]{1,20})?)\s*(?:[\u3000 \t]{1,}|\n)(.*)$')
SPEAKER_INLINE = re.compile(r'[○◯〇●]\s*([^\s○◯〇●]{1,32})[\u3000 \t]+')
HEADINGS = {'日時', '場所', '出席者', '出席委員', '欠席者', '議題', '議事', '議事録', '配布資料', '開会', '閉会', '事務局', '傍聴者'}


def parse_utterances(text: str) -> list[tuple[str, str]]:
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = re.split(r'(?m)^\s*（了）\s*$', text, maxsplit=1)[0]
    # TXT files often hard-wrap lines. Markers are still at the start of their own line.
    lines = text.split('\n')
    utterances = []
    label = None
    body = []
    started = False
    for line in lines:
        stripped = line.strip()
        match = re.match(r'^[○◯〇●][ \u3000]*([^\n]+)$', stripped)
        new_label = None
        first_body = ''
        if match:
            rest = match.group(1).strip()
            if rest not in HEADINGS:
                # Prefer ideographic spacing. For a marker-only line the whole rest is a label.
                bits = re.split(r'[ \u3000\t]+', rest, maxsplit=1)
                if len(bits) == 2 and len(bits[0]) <= 32:
                    new_label, first_body = bits[0].strip(), bits[1].strip()
                elif len(rest) <= 32 and (not re.search(r'[。、「」]', rest)):
                    new_label = rest
                if not new_label:
                    joined = re.match(r'^(.{1,32}?(?:部会長代理|部会長|委員|課長|室長|局長|審議官|副大臣|政務官|大臣|参事官(?:（[^）]+）)?))(.+)$', rest)
                    if joined:
                        new_label, first_body = joined.group(1), joined.group(2)
        if new_label and not new_label.startswith(('日時', '場所', '議題', '議事', '出席', '欠席')):
            if label and clean(' '.join(body)):
                utterances.append((label, clean(' '.join(body))))
            label, body, started = clean(new_label), [first_body] if first_body else [], True
        elif started:
            # Ignore page footer lines after closing, handled at final cleaning.
            body.append(stripped)
    if label and clean(' '.join(body)):
        utterances.append((label, clean(' '.join(body))))
    return utterances


def speaker_key(label: str) -> str:
    s = re.sub(r'[\s\u3000]+', '', label)
    s = re.sub(r'（.*?）|\(.*?\)', '', s)
    s = re.sub(r'(部会長代理|部会長|座長代理|座長|委員|参考人|課長補佐|課長|室長|局長|審議官|係長|次長|政務官|大臣|理事長|総裁|教授|会長|代理)$', '', s)
    return s or re.sub(r'[\s\u3000]+', '', label)


def build():
    OUT.mkdir(exist_ok=True)
    rows = index_rows()
    urls = sorted({url for row in rows for _, url in row['sources'] if url})
    print(f'index rows={len(rows)} source links={sum(len(r["sources"]) for r in rows)} unique urls={len(urls)}', flush=True)
    data_by_url = {}
    errors = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fetch, url): url for url in urls}
        for i, future in enumerate(as_completed(futures), 1):
            url = futures[future]
            try:
                data_by_url[url] = future.result()
            except Exception as exc:
                errors.append((url, str(exc)))
            if i % 20 == 0 or i == len(futures):
                print(f'fetch {i}/{len(futures)} errors={len(errors)}', flush=True)
    db_path = OUT / '年金部会_発言データベース.sqlite'
    if db_path.exists():
        db_path.unlink()
    db = sqlite3.connect(db_path)
    db.executescript('''
    CREATE TABLE meetings (id INTEGER PRIMARY KEY, index_order INTEGER, meeting_label TEXT, date TEXT, agenda TEXT, index_url TEXT);
    CREATE TABLE sources (id INTEGER PRIMARY KEY, meeting_id INTEGER REFERENCES meetings(id), label TEXT, url TEXT UNIQUE, format TEXT, sha256 TEXT, status TEXT, utterance_count INTEGER DEFAULT 0);
    CREATE TABLE utterances (id INTEGER PRIMARY KEY, meeting_id INTEGER REFERENCES meetings(id), source_id INTEGER REFERENCES sources(id), sequence INTEGER, speaker_label TEXT, speaker_key TEXT, body TEXT);
    CREATE INDEX utterances_by_speaker ON utterances(speaker_key, meeting_id);
    CREATE INDEX utterances_by_meeting ON utterances(meeting_id, sequence);
    CREATE VIRTUAL TABLE utterances_fts USING fts5(speaker_label, speaker_key, body, content='utterances', content_rowid='id', tokenize='trigram');
    CREATE TRIGGER utterances_ai AFTER INSERT ON utterances BEGIN INSERT INTO utterances_fts(rowid,speaker_label,speaker_key,body) VALUES (new.id,new.speaker_label,new.speaker_key,new.body); END;
    ''')
    qa = []
    for row in rows:
        cur = db.execute('INSERT INTO meetings(index_order,meeting_label,date,agenda,index_url) VALUES (?,?,?,?,?)',
                         (row['index_order'], row['meeting_label'], row['date'], row['agenda'], INDEX_URL))
        meeting_id = cur.lastrowid
        for label, url in row['sources']:
            data = data_by_url.get(url)
            fmt = urlparse(url).path.rsplit('.', 1)[-1].lower()
            status = 'ok' if data else 'fetch_error'
            cur = db.execute('INSERT OR IGNORE INTO sources(meeting_id,label,url,format,sha256,status) VALUES (?,?,?,?,?,?)',
                             (meeting_id, label, url, fmt, hashlib.sha256(data).hexdigest() if data else None, status))
            source_id = cur.lastrowid
            if not source_id:
                continue
            if not data:
                qa.append({'url': url, 'status': 'fetch_error', 'count': 0})
                continue
            text = source_text(url, data)
            utterances = parse_utterances(text)
            for seq, (speaker, body) in enumerate(utterances, 1):
                db.execute('INSERT INTO utterances(meeting_id,source_id,sequence,speaker_label,speaker_key,body) VALUES (?,?,?,?,?,?)',
                           (meeting_id, source_id, seq, speaker, speaker_key(speaker), body))
            status = 'ok' if utterances else 'no_speaker_detected'
            db.execute('UPDATE sources SET status=?,utterance_count=? WHERE id=?', (status, len(utterances), source_id))
            qa.append({'url': url, 'date': row['date'], 'meeting': row['meeting_label'], 'status': status,
                       'count': len(utterances), 'first_speaker': utterances[0][0] if utterances else '',
                       'last_speaker': utterances[-1][0] if utterances else ''})
    db.commit()
    with (OUT / '年金部会_発言一覧.csv').open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['発言ID','開催日','回次','発言順','発言者表記','発言者検索キー','発言本文','議事録URL'])
        w.writerows(db.execute('''SELECT u.id,m.date,m.meeting_label,u.sequence,u.speaker_label,u.speaker_key,u.body,s.url
                                FROM utterances u JOIN meetings m ON m.id=u.meeting_id JOIN sources s ON s.id=u.source_id
                                ORDER BY m.index_order,u.sequence'''))
    with (OUT / '議事録リンク未掲載の会合.csv').open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['開催日','回次','議題等','一覧ページURL'])
        w.writerows(db.execute('''SELECT m.date,m.meeting_label,m.agenda,m.index_url FROM meetings m
                                WHERE NOT EXISTS (SELECT 1 FROM sources s WHERE s.meeting_id=m.id)
                                ORDER BY m.index_order'''))
    (OUT / '抽出確認.json').write_text(json.dumps({'generated_at': datetime.now().isoformat(), 'index_url': INDEX_URL,
                                                 'rows': len(rows), 'sources': qa, 'fetch_errors': errors}, ensure_ascii=False, indent=2), encoding='utf-8')
    print('meetings', db.execute('SELECT count(*) FROM meetings').fetchone()[0],
          'sources', db.execute('SELECT count(*) FROM sources').fetchone()[0],
          'utterances', db.execute('SELECT count(*) FROM utterances').fetchone()[0],
          'speakers', db.execute('SELECT count(DISTINCT speaker_key) FROM utterances').fetchone()[0], flush=True)
    print('status', db.execute('SELECT status,count(*) FROM sources GROUP BY status').fetchall(), flush=True)
    print('low_count', [x for x in qa if x['count'] < 5], flush=True)
    db.close()


if __name__ == '__main__':
    build()
