from flask import Flask, render_template, request, jsonify, send_file, flash, redirect, url_for, session
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import re
import csv
import io
import os
from datetime import datetime, timedelta
import threading
import time
from urllib.parse import urlparse
import logging
from collections import defaultdict
import secrets

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max file size for large batches
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)

# Global storage with thread safety
analysis_progress = {}
analysis_results = {}
task_control = {}
task_locks = defaultdict(threading.Lock)
global_lock = threading.Lock()

# Cleanup old results (runs in background)
def cleanup_old_results():
    """Remove results older than 24 hours to prevent memory bloat."""
    while True:
        time.sleep(3600)  # Run every hour
        current_time = time.time()
        with global_lock:
            # Find tasks older than 24 hours
            old_tasks = [
                tid for tid, progress in analysis_progress.items()
                if current_time - int(tid) > 86400  # 24 hours
            ]
            for tid in old_tasks:
                analysis_progress.pop(tid, None)
                analysis_results.pop(tid, None)
                task_control.pop(tid, None)
                if tid in task_locks:
                    del task_locks[tid]
            if old_tasks:
                logger.info(f"Cleaned up {len(old_tasks)} old tasks")

# Start cleanup thread
cleanup_thread = threading.Thread(target=cleanup_old_results, daemon=True)
cleanup_thread.start()


class ChatbotDetector:
    def __init__(self):
        self.shopify_patterns = [
            r'https://cdn\.shopify\.com/extensions/[a-f0-9-]+/inbox-\d+/assets/inbox-chat-loader\.js',
            r'shopify-inbox',
            r'web\.shopify\.com/inbox'
        ]

        self.other_chatbot_patterns = {
            'Tidio': [r'tidio\.co', r'code\.tidio\.co'],
            'Zendesk': [r'zdassets\.com', r'zendesk\.com.*chat'],
            'Intercom': [r'widget\.intercom\.io', r'intercom\.com.*widget'],
            'Drift': [r'js\.driftt\.com', r'drift\.com'],
            'ChatBot.com': [r'chatbot\.com'],
            'Gorgias': [r'gorgias\.com.*chat'],
            'Crisp': [r'crisp\.chat'],
            'LiveChat': [r'livechatinc\.com', r'livechat\.com'],
            'Freshchat': [r'freshchat\.com', r'freshworks\.com.*chat'],
            'Chatty': [r'https://cdn\.shopify\.com/extensions/[a-f0-9-]+/chatty-\d+/assets/chatty\.js'],
            'Willdesk': [r'https://cdn\.shopify\.com/extensions/[a-f0-9-]+/willdesk-live-chat-helpdesk-\d+/assets/willdesk\.min\.js'],
            'Smartbot': [r'https://cdn\.shopify\.com/extensions/[a-f0-9-]+/smartbot-\d+/assets/st_p\.js']
        }

        # Create session with connection pooling and retry logic
        self.session = self._create_session()

    def _create_session(self):
        """Create requests session with connection pooling and automatic retries."""
        session = requests.Session()
        
        # Retry strategy
        retry_strategy = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"]
        )
        
        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=100,  # Support concurrent requests
            pool_maxsize=100
        )
        
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        
        return session

    def normalize_url(self, url):
        """Normalize and validate URL."""
        url = url.strip()
        if not url:
            return None
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        return url

    def fetch_website_source(self, url, timeout=8):
        """Fetch website source with optimized timeout and error handling."""
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
            return None, "Request timeout"
        except requests.exceptions.ConnectionError:
            return None, "Connection failed"
        except requests.exceptions.HTTPError as e:
            return None, f"HTTP {e.response.status_code}"
        except Exception as e:
            return None, str(e)[:100]  # Truncate long errors

    def detect_chatbots(self, html_content):
        """Detect chatbots in HTML content."""
        results = {
            'shopify_inbox': False,
            'shopify_details': [],
            'other_chatbots': [],
            'total_chatbots': 0
        }

        # Check for Shopify Inbox
        for pattern in self.shopify_patterns:
            if re.search(pattern, html_content, re.IGNORECASE):
                results['shopify_inbox'] = True
                break

        # Check for other chatbots
        for chatbot_name, patterns in self.other_chatbot_patterns.items():
            for pattern in patterns:
                if re.search(pattern, html_content, re.IGNORECASE):
                    if chatbot_name not in results['other_chatbots']:
                        results['other_chatbots'].append(chatbot_name)
                    break

        results['total_chatbots'] = len(results['other_chatbots']) + (1 if results['shopify_inbox'] else 0)
        return results

    def analyze_single_website(self, url):
        """Analyze a single website for chatbot presence."""
        normalized_url = self.normalize_url(url)
        if not normalized_url:
            return {
                'url': url,
                'status': 'error',
                'error': 'Invalid URL',
                'shopify_inbox': False,
                'other_chatbots': [],
                'total_chatbots': 0,
                'analyzed_at': datetime.now().isoformat()
            }

        html_content, error = self.fetch_website_source(normalized_url)

        if error:
            return {
                'url': url,
                'status': 'error',
                'error': error,
                'shopify_inbox': False,
                'other_chatbots': [],
                'total_chatbots': 0,
                'analyzed_at': datetime.now().isoformat()
            }

        detection_results = self.detect_chatbots(html_content)

        return {
            'url': url,
            'status': 'success',
            'shopify_inbox': detection_results['shopify_inbox'],
            'shopify_details': detection_results['shopify_details'],
            'other_chatbots': detection_results['other_chatbots'],
            'total_chatbots': detection_results['total_chatbots'],
            'analyzed_at': datetime.now().isoformat()
        }


detector = ChatbotDetector()


# ─────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────

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

    task_id = str(int(time.time() * 1000))  # More unique with milliseconds

    # Read and validate file content
    try:
        file_content = file.read()
        filename = file.filename
    except Exception as e:
        flash(f'Error reading file: {str(e)}')
        return redirect(url_for('index'))

    with global_lock:
        task_control[task_id] = 'running'

    thread = threading.Thread(
        target=process_bulk_analysis,
        args=(file_content, filename, task_id)
    )
    thread.daemon = True
    thread.start()

    return redirect(url_for('bulk_progress', task_id=task_id))


def process_bulk_analysis(file_content, filename, task_id):
    """Process bulk analysis with optimized threading and error handling."""
    try:
        content = file_content.decode('utf-8')
        urls = []

        # Parse file
        if filename.lower().endswith('.csv'):
            csv_reader = csv.reader(io.StringIO(content))
            for row in csv_reader:
                if row and row[0].strip():
                    urls.append(row[0].strip())
        else:
            urls = [line.strip() for line in content.split('\n') if line.strip()]

        # Remove duplicates while preserving order
        seen = set()
        unique_urls = []
        for url in urls:
            if url not in seen:
                seen.add(url)
                unique_urls.append(url)
        urls = unique_urls

        total_urls = len(urls)
        
        with global_lock:
            analysis_progress[task_id] = {
                'current': 0,
                'total': total_urls,
                'status': 'processing',
                'successful': 0,
                'errors': 0
            }
            analysis_results[task_id] = []

        # Process URLs with adaptive batch sizing
        batch_size = min(10, max(1, total_urls // 100))  # Adaptive batching
        
        for i, url in enumerate(urls):
            # Check control state
            with task_locks[task_id]:
                ctrl = task_control.get(task_id, 'running')

            if ctrl == 'stopped':
                with global_lock:
                    analysis_progress[task_id]['status'] = 'stopped'
                break

            # Pause loop
            while True:
                with task_locks[task_id]:
                    ctrl = task_control.get(task_id, 'running')
                if ctrl == 'stopped':
                    break
                if ctrl == 'running':
                    break
                with global_lock:
                    analysis_progress[task_id]['status'] = 'paused'
                time.sleep(0.5)

            # Re-check after pause
            with task_locks[task_id]:
                ctrl = task_control.get(task_id, 'running')
            if ctrl == 'stopped':
                with global_lock:
                    analysis_progress[task_id]['status'] = 'stopped'
                break

            with global_lock:
                analysis_progress[task_id]['status'] = 'processing'

            if url:
                result = detector.analyze_single_website(url)
                
                with global_lock:
                    analysis_results[task_id].append(result)
                    if result['status'] == 'success':
                        analysis_progress[task_id]['successful'] += 1
                    else:
                        analysis_progress[task_id]['errors'] += 1

            with global_lock:
                analysis_progress[task_id]['current'] = i + 1

            # Adaptive delay to prevent overwhelming servers
            if i % batch_size == 0 and i > 0:
                time.sleep(0.2)
            else:
                time.sleep(0.05)

        # Mark as completed if not stopped
        with task_locks[task_id]:
            ctrl = task_control.get(task_id, 'running')
        if ctrl != 'stopped':
            with global_lock:
                analysis_progress[task_id]['status'] = 'completed'

        logger.info(f"Task {task_id} completed: {total_urls} URLs processed")

    except Exception as e:
        logger.error(f"Error in bulk analysis {task_id}: {str(e)}")
        with global_lock:
            analysis_progress[task_id] = {
                'current': 0, 'total': 0,
                'status': 'error', 'error': str(e)
            }


# ─────────────────────────────────────────────────────────────
# Control API
# ─────────────────────────────────────────────────────────────

@app.route('/api/control/<task_id>/<action>', methods=['POST'])
def api_control(task_id, action):
    """Control task execution: pause, resume, stop."""
    if task_id not in task_control:
        return jsonify({'success': False, 'error': 'Task not found'}), 404

    allowed = {'pause', 'resume', 'stop'}
    if action not in allowed:
        return jsonify({'success': False, 'error': 'Invalid action'}), 400

    with task_locks[task_id]:
        if action == 'pause':
            task_control[task_id] = 'paused'
        elif action == 'resume':
            task_control[task_id] = 'running'
        elif action == 'stop':
            task_control[task_id] = 'stopped'

    return jsonify({'success': True, 'task_id': task_id, 'action': action})


@app.route('/bulk_progress/<task_id>')
def bulk_progress(task_id):
    return render_template('bulk_progress.html', task_id=task_id)


@app.route('/api/progress/<task_id>')
def api_progress(task_id):
    """Get progress for a specific task."""
    with global_lock:
        progress = analysis_progress.get(task_id, {
            'current': 0, 'total': 0, 'status': 'not_found'
        }).copy()
    
    with task_locks[task_id]:
        progress['control'] = task_control.get(task_id, 'unknown')
    
    return jsonify(progress)


@app.route('/bulk_results/<task_id>')
def bulk_results(task_id):
    with global_lock:
        if task_id not in analysis_results:
            flash('Results not found or expired')
            return redirect(url_for('index'))
        results = analysis_results[task_id].copy()
    
    return render_template('bulk_results.html', results=results, task_id=task_id)


# ─────────────────────────────────────────────────────────────
# Download helpers
# ─────────────────────────────────────────────────────────────

def _build_csv(results):
    """Build CSV file from results."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        'URL', 'Status', 'Shopify Inbox Detected', 'Other Chatbots',
        'Total Chatbots', 'Error', 'Analyzed At'
    ])
    for result in results:
        writer.writerow([
            result['url'],
            result['status'],
            'Yes' if result.get('shopify_inbox') else 'No',
            ', '.join(result.get('other_chatbots', [])) if result.get('other_chatbots') else 'None',
            result.get('total_chatbots', 0),
            result.get('error', ''),
            result.get('analyzed_at', '')
        ])

    output.seek(0)
    file_output = io.BytesIO()
    file_output.write(output.getvalue().encode('utf-8'))
    file_output.seek(0)
    return file_output


@app.route('/download_results/<task_id>')
def download_results(task_id):
    """Download all results as CSV."""
    with global_lock:
        if task_id not in analysis_results:
            flash('Results not found')
            return redirect(url_for('index'))
        results = analysis_results[task_id].copy()

    file_output = _build_csv(results)
    return send_file(
        file_output,
        mimetype='text/csv',
        as_attachment=True,
        download_name=f'chatbot_analysis_{task_id}.csv'
    )


@app.route('/download_results/<task_id>/with_shopify_inbox')
def download_with_shopify_inbox(task_id):
    """Download only stores with Shopify Inbox as CSV."""
    with global_lock:
        if task_id not in analysis_results:
            flash('Results not found')
            return redirect(url_for('index'))
        filtered = [r for r in analysis_results[task_id] if r.get('shopify_inbox')]

    file_output = _build_csv(filtered)
    return send_file(
        file_output,
        mimetype='text/csv',
        as_attachment=True,
        download_name=f'with_shopify_inbox_{task_id}.csv'
    )


@app.route('/download_results/<task_id>/without_shopify_inbox')
def download_without_shopify_inbox(task_id):
    """Download only stores without Shopify Inbox as CSV."""
    with global_lock:
        if task_id not in analysis_results:
            flash('Results not found')
            return redirect(url_for('index'))
        filtered = [r for r in analysis_results[task_id] if not r.get('shopify_inbox')]

    file_output = _build_csv(filtered)
    return send_file(
        file_output,
        mimetype='text/csv',
        as_attachment=True,
        download_name=f'without_shopify_inbox_{task_id}.csv'
    )


# ─────────────────────────────────────────────────────────────
# TXT Downloads (URLs only)
# ─────────────────────────────────────────────────────────────

@app.route('/download_results/<task_id>/with_shopify_inbox/txt')
def download_with_shopify_inbox_txt(task_id):
    """Download TXT with URLs that have Shopify Inbox."""
    with global_lock:
        if task_id not in analysis_results:
            flash('Results not found')
            return redirect(url_for('index'))
        filtered = [r for r in analysis_results[task_id] if r.get('shopify_inbox')]

    urls = [r['url'] for r in filtered]
    output = io.StringIO()
    for url in urls:
        output.write(url + '\n')

    output.seek(0)
    file_output = io.BytesIO()
    file_output.write(output.getvalue().encode('utf-8'))
    file_output.seek(0)

    return send_file(
        file_output,
        mimetype='text/plain',
        as_attachment=True,
        download_name=f'with_shopify_inbox_{task_id}.txt'
    )


@app.route('/download_results/<task_id>/without_shopify_inbox/txt')
def download_without_shopify_inbox_txt(task_id):
    """Download TXT with URLs that don't have Shopify Inbox."""
    with global_lock:
        if task_id not in analysis_results:
            flash('Results not found')
            return redirect(url_for('index'))
        filtered = [r for r in analysis_results[task_id] if not r.get('shopify_inbox')]

    urls = [r['url'] for r in filtered]
    output = io.StringIO()
    for url in urls:
        output.write(url + '\n')

    output.seek(0)
    file_output = io.BytesIO()
    file_output.write(output.getvalue().encode('utf-8'))
    file_output.seek(0)

    return send_file(
        file_output,
        mimetype='text/plain',
        as_attachment=True,
        download_name=f'without_shopify_inbox_{task_id}.txt'
    )


# ─────────────────────────────────────────────────────────────
# Re-analyze
# ─────────────────────────────────────────────────────────────

@app.route('/reanalyze/<task_id>/without_shopify_inbox', methods=['POST'])
def reanalyze_without_shopify_inbox(task_id):
    """Re-analyze stores without Shopify Inbox."""
    with global_lock:
        if task_id not in analysis_results:
            flash('Original results not found')
            return redirect(url_for('index'))
        filtered = [r for r in analysis_results[task_id] if not r.get('shopify_inbox')]

    if not filtered:
        flash('No stores without Shopify Inbox found')
        return redirect(url_for('bulk_results', task_id=task_id))

    new_task_id = str(int(time.time() * 1000))
    urls = [r['url'] for r in filtered]
    url_content = '\n'.join(urls)
    file_content = url_content.encode('utf-8')
    filename = 'reanalysis.txt'

    with global_lock:
        task_control[new_task_id] = 'running'

    thread = threading.Thread(
        target=process_bulk_analysis,
        args=(file_content, filename, new_task_id)
    )
    thread.daemon = True
    thread.start()

    flash(f'Re-analyzing {len(urls)} stores...')
    return redirect(url_for('bulk_progress', task_id=new_task_id))


# ─────────────────────────────────────────────────────────────
# Health check for monitoring
# ─────────────────────────────────────────────────────────────

@app.route('/health')
def health():
    """Health check endpoint for monitoring."""
    with global_lock:
        active_tasks = len([p for p in analysis_progress.values() if p.get('status') == 'processing'])
    
    return jsonify({
        'status': 'healthy',
        'active_tasks': active_tasks,
        'timestamp': datetime.now().isoformat()
    })


if __name__ == '__main__':
    # Production server should use gunicorn
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port, debug=False)
