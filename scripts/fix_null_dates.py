"""Re-extract dates for NULL-date archive files using updated extraction patterns."""
import glob, os, re, hashlib, sqlite3, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.sources.scraper import _fetch_html as fetch_html
from app.sources.extractor import _date_from_html, extract_from_html
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

_TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "ref", "source", "si"}

def normalized_url(url: str) -> str:
    parts = urlparse(url)
    query = [(k, v) for k, v in parse_qsl(parts.query)
             if k.lower() not in _TRACKING_PARAMS]
    path = parts.path.rstrip("/") or "/"
    return urlunparse(parts._replace(path=path, query=urlencode(query), fragment=""))

def fingerprint(url: str) -> str:
    return hashlib.sha256(normalized_url(url).encode("utf-8")).hexdigest()

def parse_published_at(val) -> str | None:
    """Return ISO datetime string or None. Accepts str or datetime."""
    if val is None:
        return None
    if isinstance(val, datetime):
        if val.tzinfo is None:
            val = val.replace(tzinfo=timezone.utc)
        return val.isoformat()
    if isinstance(val, str):
        if not val or val.lower() in ('none', 'null', ''):
            return None
        try:
            from app.sources.date_parser import parse_date
            dt = parse_date(val)
            if dt:
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.isoformat()
        except Exception:
            pass
    return None

def read_front_matter(path: str):
    """Read archive file and return (front_matter_dict, body_text, raw_content)."""
    with open(path, 'r', encoding='utf-8') as f:
        raw = f.read()
    
    m = re.match(r'^---\n(.*?)\n---\n', raw, re.DOTALL)
    if not m:
        return None, raw, raw
    
    fm_text = m.group(1)
    body = raw[m.end():]
    
    fm = {}
    for line in fm_text.split('\n'):
        if ': ' in line:
            key, _, val = line.partition(': ')
            fm[key.strip()] = val.strip()
    
    return fm, body, raw

def write_front_matter(path: str, fm: dict, body: str):
    """Write updated front-matter + body back to file."""
    lines = ['---']
    for k, v in fm.items():
        if v is not None:
            lines.append(f'{k}: {v}')
        else:
            lines.append(f'{k}: null')
    lines.append('---')
    lines.append('')
    lines.append(body)
    
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))

def main():
    db = sqlite3.connect('data/media.db')
    
    # Find all archive files that have NULL published_at
    archive_files = glob.glob('data/archive/**/*.md', recursive=True)
    null_date_files = []
    
    for fpath in archive_files:
        with open(fpath, 'r', encoding='utf-8') as f:
            c = f.read()
        m = re.search(r'^published_at: (.+)$', c, re.MULTILINE)
        if m:
            val = m.group(1).strip().strip("'").strip('"')
            if val.lower() in ('null', 'none', ''):
                null_date_files.append(fpath)
    
    print(f'Found {len(null_date_files)} archive files with NULL published_at')
    
    fixed = 0
    failed_urls = []
    
    for fpath in null_date_files:
        fm, body, raw = read_front_matter(fpath)
        if not fm:
            print(f'  SKIP (no front-matter): {os.path.basename(fpath)}')
            continue
        
        url = fm.get('url', '')
        if not url:
            print(f'  SKIP (no URL): {os.path.basename(fpath)}')
            continue
        
        basename = os.path.basename(fpath)
        print(f'\n  Processing: {basename}')
        print(f'    URL: {url[:70]}')
        
        try:
            html = fetch_html(url, render_js=True, timeout=25)
        except Exception as e:
            print(f'    FETCH FAILED: {e}')
            failed_urls.append(url)
            continue
        
        if not html or len(html) < 500:
            print(f'    HTML too short ({len(html) if html else 0} bytes), trying without JS')
            try:
                html = fetch_html(url, render_js=False, timeout=25)
            except Exception as e:
                print(f'    FETCH FAILED (no JS): {e}')
                failed_urls.append(url)
                continue
        
        # Re-extract date
        extracted = extract_from_html(html, url)
        new_date = extracted.get('published_at')
        
        if new_date:
            # Format as ISO datetime
            dt_str = parse_published_at(new_date)
            if dt_str:
                print(f'    [FOUND] date: {dt_str}')
                fm['published_at'] = dt_str
                write_front_matter(fpath, fm, body)
                
                # Re-insert into DB
                fp = fingerprint(url)
                title = fm.get('title', '')
                source_name = fm.get('source_name', '')
                source_type = fm.get('source_type', 'scrape')
                topic = fm.get('topic', 'uncategorized')
                
                # Check if already exists
                exists = db.execute("SELECT 1 FROM articles WHERE fingerprint=?", (fp,)).fetchone()
                if not exists:
                    # Use existing archive file path relative to data dir
                    rel_path = os.path.relpath(fpath, 'data').replace('\\', '/')
                    now = datetime.now(timezone.utc).isoformat()
                    db.execute("""
                        INSERT INTO articles (fingerprint, url, title, published_at, source_name, source_type, topic, fetched_at, archive_path, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'archived')
                    """, (fp, url, title, dt_str, source_name, source_type, topic, now, rel_path))
                    db.commit()
                    print(f'    [OK] Inserted into DB')
                else:
                    db.execute("UPDATE articles SET published_at=? WHERE fingerprint=?", (dt_str, fp))
                    db.commit()
                    print(f'    [OK] Updated DB record')
                fixed += 1
                continue
        
        date_from_html = _date_from_html(html)
        if date_from_html:
            dt_str = parse_published_at(date_from_html)
            if dt_str:
                print(f'    [FOUND] date via _date_from_html: {dt_str}')
                fm['published_at'] = dt_str
                write_front_matter(fpath, fm, body)
                fp = fingerprint(url)
                title = fm.get('title', '')
                source_name = fm.get('source_name', '')
                source_type = fm.get('source_type', 'scrape')
                topic = fm.get('topic', 'uncategorized')
                exists = db.execute("SELECT 1 FROM articles WHERE fingerprint=?", (fp,)).fetchone()
                if not exists:
                    rel_path = os.path.relpath(fpath, 'data').replace('\\', '/')
                    now = datetime.now(timezone.utc).isoformat()
                    db.execute("""
                        INSERT INTO articles (fingerprint, url, title, published_at, source_name, source_type, topic, fetched_at, archive_path, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'archived')
                    """, (fp, url, title, dt_str, source_name, source_type, topic, now, rel_path))
                    db.commit()
                    print(f'    [OK] Inserted into DB')
                fixed += 1
                continue
        
        print(f'    [FAIL] No date found')
        failed_urls.append(url)
    
    print(f'\n\n=== Summary ===')
    print(f'Fixed: {fixed} articles')
    print(f'Failed: {len(failed_urls)} articles')
    for url in failed_urls:
        print(f'  ❌ {url}')
    
    db.close()

if __name__ == '__main__':
    main()
