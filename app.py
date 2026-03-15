from flask import Flask, render_template, request, jsonify, send_file, flash, redirect, url_for
import re
import csv
import io
import os
import json
from datetime import datetime, timedelta
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
import logging
import secrets
import psycopg2
import psycopg2.extras
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── App ────────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY')
if not app.secret_key:
    raise RuntimeError('SECRET_KEY environment variable must be set')

app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)

# ─── Database ────────────────────────────────────────────────────────────────
DATABASE_URL = os.environ.get('DATABASE_URL')
if not DATABASE_URL:
    raise RuntimeError('DATABASE_URL environment variable must be set')


def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    return conn


def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id     TEXT PRIMARY KEY,
                    status      TEXT NOT NULL DEFAULT 'processing',
                    current     INTEGER DEFAULT 0,
                    total       INTEGER DEFAULT 0,
                    successful  INTEGER DEFAULT 0,
                    errors      INTEGER DEFAULT 0,
                    error_msg   TEXT,
                    control     TEXT DEFAULT 'running',
                    created_at  TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS task_results (
                    id          SERIAL PRIMARY KEY,
                    task_id     TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                    result      JSONB NOT NULL,
                    created_at  TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_task_results_task_id
                    ON task_results(task_id);
            """)
    logger.info("Database initialised")


init_db()

# ─── DB helpers ──────────────────────────────────────────────────────────────

def db_create_task(task_id, total):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO tasks (task_id, status, total, control)
                VALUES (%s, 'processing', %s, 'running')
                ON CONFLICT (task_id) DO NOTHING
            """, (task_id, total))


def db_update_progress(task_id, current, successful, errors, status='processing'):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE tasks SET current=%s, successful=%s, errors=%s, status=%s
                WHERE task_id=%s
            """, (current, successful, errors, status, task_id))


def db_append_result(task_id, result):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO task_results (task_id, result) VALUES (%s, %s)",
                (task_id, json.dumps(result))
            )


def db_get_progress(task_id):
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM tasks WHERE task_id=%s", (task_id,))
            row = cur.fetchone()
            if not row:
                return {'current': 0, 'total': 0, 'status': 'not_found'}
            return dict(row)


def db_get_results(task_id):
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT result FROM task_results WHERE task_id=%s ORDER BY id",
                (task_id,)
            )
            return [row['result'] for row in cur.fetchall()]


def db_get_control(task_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT control FROM tasks WHERE task_id=%s", (task_id,))
            row = cur.fetchone()
            return row[0] if row else 'running'


def db_set_control(task_id, value):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET control=%s WHERE task_id=%s",
                (value, task_id)
            )


def db_task_exists(task_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM tasks WHERE task_id=%s", (task_id,))
            return cur.fetchone() is not None


# ─── Background cleanup ──────────────────────────────────────────────────────

def cleanup_old_results():
    while True:
        time.sleep(3600)
        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM tasks WHERE created_at < NOW() - INTERVAL '24 hours'"
                    )
            logger.info("Cleaned up old tasks from database")
        except Exception as e:
            logger.error(f"Cleanup error: {e}")


cleanup_thread = threading.Thread(target=cleanup_old_results, daemon=True)
cleanup_thread.start()


# ─── ChatbotDetector ─────────────────────────────────────────────────────────

class ChatbotDetector:
    def __init__(self):
        self.shopify_patterns = [
            r'https://cdn\.shopify\.com/extensions/[a-f0-9-]+/inbox-\d+/assets/inbox-chat-loader\.js',
            r'shopify-inbox',
            r'web\.shopify\.com/inbox',
        ]
        self.other_chatbot_patterns = {
            'Tidio':       [r'tidio\.co', r'code\.tidio\.co'],
            'Zendesk':     [r'zdassets\.com', r'zendesk\.com.*chat'],
            'Intercom':    [r'widget\.intercom\.io', r'intercom\.com.*widget'],
            'Drift':       [r'js\.driftt\.com', r'drift\.com'],
            'ChatBot.com': [r'chatbot\.com'],
            'Gorgias':     [r'gorgias\.com.*chat'],
            'Crisp':       [r'crisp\.chat'],
            'LiveChat':    [r'livechatinc\.com', r'livechat\.com'],
            'Freshchat':   [r'freshchat\.com', r'freshworks\.com.*chat'],
            'Chatty':      [r'https://cdn\.shopify\.com/extensions/[a-f0-9-]+/chatty-\d+/assets/chatty\.js'],
            'Willdesk':    [r'https://cdn\.shopify\.com/extensions/[a-f0-9-]+/willdesk-live-chat-helpdesk-\d+/assets/willdesk\.min\.js'],
            'Smartbot':    [r'https://cdn\.shopify\.com/extensions/[a-f0-9-]+/smartbot-\d+/assets/st_p\.js'],
        }
        self.session = self._create_session()

    def _create_session(self):
        session = requests.Session()
        retry = Retry(
            total=1,
            backoff_factor=0.3,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=['GET']
        )
        adapter = HTTPAdapter(
            max_retries=retry,
            pool_connections=50,
            pool_maxsize=50
        )
        session.mount('http://', adapter)
        session.mount('https://', adapter)
        return session

    def normalize_url(self, url):
        url = url.strip()
        if not url:
            return None
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        return url

    def fetch_website_source(self, url, timeout=5):
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
                'Accept-Encoding': 'gzip, deflate',
                'Connection': 'keep-alive',
            }
            response = self.session.get(url, headers=headers, timeout=timeout, allow_redirects=True)
            response.raise_for_status()
            return response.text, None
        except requests.exceptions.Timeout:
            return None, 'Request timeout'
        except requests.exceptions.ConnectionError:
            return None, 'Connection failed'
        except requests.exceptions.HTTPError as e:
            return None, f'HTTP {e.response.status_code}'
        except Exception as e:
            return None, str(e)[:100]

    def detect_chatbots(self, html_content):
        results = {
            'shopify_inbox': False,
            'other_chatbots': [],
            'total_chatbots': 0
        }
        for pattern in self.shopify_patterns:
            if re.search(pattern, html_content, re.IGNORECASE):
                results['shopify_inbox'] = True
                break
        for name, patterns in self.other_chatbot_patterns.items():
            for pattern in patterns:
                if re.search(pattern, html_content, re.IGNORECASE):
                    results['other_chatbots'].append(name)
                    break
        results['total_chatbots'] = len(results['other_chatbots']) + (1 if results['shopify_inbox'] else 0)
        return results

    def analyze_single_website(self, url):
        normalized = self.normalize_url(url)
        if not normalized:
            return {
                'url': url, 'status': 'error', 'error': 'Invalid URL',
                'shopify_inbox': False, 'other_chatbots': [],
                'total_chatbots': 0, 'analyzed_at': datetime.now().isoformat()
            }
        html, error = self.fetch_website_source(normalized)
        if error:
            return {
                'url': url, 'status': 'error', 'error': error,
                'shopify_inbox': False, 'other_chatbots': [],
                'total_chatbots': 0, 'analyzed_at': datetime.now().isoformat()
            }
        detection = self.detect_chatbots(html)
        return {
            'url': url, 'status': 'success',
            'shopify_inbox': detection['shopify_inbox'],
            'other_chatbots': detection['other_chatbots'],
            'total_chatbots': detection['total_chatbots'],
            'analyzed_at': datetime.now().isoformat()
        }


detector = ChatbotDetector()


# ─── Bulk processor ──────────────────────────────────────────────────────────

def process_bulk_analysis(file_content, filename, task_id):
    try:
        content = file_content.decode('utf-8')

        if filename.lower().endswith('.csv'):
            csv_reader = csv.reader(io.StringIO(content))
            urls = [row[0].strip() for row in csv_reader if row and row[0].strip()]
        else:
            urls = [line.strip() for line in content.split('\n') if line.strip()]

        # Deduplicate preserving order
        seen = set()
        urls = [u for u in urls if not (u in seen or seen.add(u))]

        total_urls = len(urls)
        db_create_task(task_id, total_urls)

        completed_count = 0
        successful = 0
        errors = 0
        WORKERS = 25

        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            future_to_url = {
                executor.submit(detector.analyze_single_website, url): url
                for url in urls
            }

            for future in as_completed(future_to_url):
                # Pause / stop check
                while True:
                    ctrl = db_get_control(task_id)
                    if ctrl == 'stopped':
                        executor.shutdown(wait=False, cancel_futures=True)
                        db_update_progress(task_id, completed_count, successful, errors, 'stopped')
                        return
                    if ctrl == 'running':
                        break
                    db_update_progress(task_id, completed_count, successful, errors, 'paused')
                    time.sleep(0.5)

                db_update_progress(task_id, completed_count, successful, errors, 'processing')

                try:
                    result = future.result()
                except Exception as e:
                    url = future_to_url[future]
                    result = {
                        'url': url, 'status': 'error', 'error': str(e),
                        'shopify_inbox': False, 'other_chatbots': [],
                        'total_chatbots': 0, 'analyzed_at': datetime.now().isoformat()
                    }

                completed_count += 1
                if result['status'] == 'success':
                    successful += 1
                else:
                    errors += 1

                db_append_result(task_id, result)
                db_update_progress(task_id, completed_count, successful, errors, 'processing')

        db_update_progress(task_id, completed_count, successful, errors, 'completed')
        logger.info(f"Task {task_id} completed: {total_urls} URLs")

    except Exception as e:
        logger.error(f"Bulk analysis error {task_id}: {e}")
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE tasks SET status='error', error_msg=%s WHERE task_id=%s",
                    (str(e), task_id)
                )


# ─── Routes ──────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/analyze_single', methods=['POST'])
def analyze_single():
    url = request.form.get('url', '').strip()
    if not url:
        flash('Please enter a URL')
        return redirect(url_for('index'))
    result = detector.analyze_single_website(url)
    return render_template('single_result.html', result=result)


@app.route('/analyze_bulk', methods=['POST'])
def analyze_bulk():
    if 'file' not in request.files:
        flash('No file uploaded')
        return redirect(url_for('index'))
    file = request.files['file']
    if file.filename == '':
        flash('No file selected')
        return redirect(url_for('index'))
    if not file.filename.lower().endswith(('.csv', '.txt')):
        flash('Please upload a CSV or TXT file')
        return redirect(url_for('index'))

    task_id = str(int(time.time() * 1000))
    try:
        file_content = file.read()
        filename = file.filename
    except Exception as e:
        flash(f'Error reading file: {str(e)}')
        return redirect(url_for('index'))

    thread = threading.Thread(
        target=process_bulk_analysis,
        args=(file_content, filename, task_id),
        daemon=True
    )
    thread.start()

    return redirect(url_for('bulk_progress', task_id=task_id))


@app.route('/bulk_progress/<task_id>')
def bulk_progress(task_id):
    return render_template('bulk_progress.html', task_id=task_id)


@app.route('/api/progress/<task_id>')
def api_progress(task_id):
    return jsonify(db_get_progress(task_id))


@app.route('/api/control/<task_id>/<action>', methods=['POST'])
def api_control(task_id, action):
    if not db_task_exists(task_id):
        return jsonify({'success': False, 'error': 'Task not found'}), 404
    if action not in {'pause', 'resume', 'stop'}:
        return jsonify({'success': False, 'error': 'Invalid action'}), 400
    value = 'running' if action == 'resume' else action
    db_set_control(task_id, value)
    return jsonify({'success': True, 'task_id': task_id, 'action': action})


@app.route('/bulk_results/<task_id>')
def bulk_results(task_id):
    if not db_task_exists(task_id):
        flash('Results not found or expired')
        return redirect(url_for('index'))
    results = db_get_results(task_id)
    return render_template('bulk_results.html', results=results, task_id=task_id)


# ─── Downloads ───────────────────────────────────────────────────────────────

def _build_csv(results):
    """CSV: URL, Status, Shopify Inbox, Other Chatbots (count only), Total Chatbots, Error, Analysed At."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['URL', 'Status', 'Shopify Inbox', 'Other Chatbots Detected',
                     'Total Chatbots', 'Error', 'Analysed At'])
    for r in results:
        other_count = len(r.get('other_chatbots', []))
        writer.writerow([
            r['url'],
            r['status'],
            'Yes' if r.get('shopify_inbox') else 'No',
            other_count,                          # count only, no names
            r.get('total_chatbots', 0),
            r.get('error', ''),
            r.get('analyzed_at', '')
        ])
    output.seek(0)
    buf = io.BytesIO()
    buf.write(output.getvalue().encode('utf-8'))
    buf.seek(0)
    return buf


def _build_txt(results):
    """Plain text: one URL per line."""
    output = io.StringIO()
    for r in results:
        output.write(r['url'] + '\n')
    output.seek(0)
    buf = io.BytesIO()
    buf.write(output.getvalue().encode('utf-8'))
    buf.seek(0)
    return buf


@app.route('/download_results/<task_id>')
def download_results(task_id):
    """All results — CSV."""
    if not db_task_exists(task_id):
        flash('Results not found')
        return redirect(url_for('index'))
    results = db_get_results(task_id)
    return send_file(_build_csv(results), mimetype='text/csv', as_attachment=True,
                     download_name=f'chatbot_analysis_{task_id}.csv')


@app.route('/download_results/<task_id>/with_shopify_inbox/txt')
def download_with_shopify_inbox_txt(task_id):
    """Stores WITH Shopify Inbox — TXT (URLs only)."""
    if not db_task_exists(task_id):
        flash('Results not found')
        return redirect(url_for('index'))
    filtered = [r for r in db_get_results(task_id) if r.get('shopify_inbox')]
    return send_file(_build_txt(filtered), mimetype='text/plain', as_attachment=True,
                     download_name=f'with_shopify_inbox_{task_id}.txt')


@app.route('/download_results/<task_id>/without_shopify_inbox/txt')
def download_without_shopify_inbox_txt(task_id):
    """Stores WITHOUT Shopify Inbox — TXT (URLs only)."""
    if not db_task_exists(task_id):
        flash('Results not found')
        return redirect(url_for('index'))
    filtered = [r for r in db_get_results(task_id) if not r.get('shopify_inbox')]
    return send_file(_build_txt(filtered), mimetype='text/plain', as_attachment=True,
                     download_name=f'without_shopify_inbox_{task_id}.txt')


# ─── Re-analyze ──────────────────────────────────────────────────────────────

@app.route('/reanalyze/<task_id>/without_shopify_inbox', methods=['POST'])
def reanalyze_without_shopify_inbox(task_id):
    if not db_task_exists(task_id):
        flash('Original results not found')
        return redirect(url_for('index'))
    filtered = [r for r in db_get_results(task_id) if not r.get('shopify_inbox')]
    if not filtered:
        flash('No stores without Shopify Inbox found')
        return redirect(url_for('bulk_results', task_id=task_id))

    new_task_id = str(int(time.time() * 1000))
    urls = [r['url'] for r in filtered]
    file_content = '\n'.join(urls).encode('utf-8')

    thread = threading.Thread(
        target=process_bulk_analysis,
        args=(file_content, 'reanalysis.txt', new_task_id),
        daemon=True
    )
    thread.start()

    flash(f'Re-analyzing {len(urls)} stores…')
    return redirect(url_for('bulk_progress', task_id=new_task_id))


# ─── Health check ────────────────────────────────────────────────────────────

@app.route('/health')
def health():
    return jsonify({'status': 'healthy', 'timestamp': datetime.now().isoformat()})


# ─── Context processor ───────────────────────────────────────────────────────

@app.context_processor
def inject_year():
    return {'current_year': datetime.now().year}


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port, debug=False)
