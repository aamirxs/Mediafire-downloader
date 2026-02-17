from flask import Flask, request, jsonify, render_template
import os
from mediafire_downloader import MediaFireDownloader

app = Flask(__name__)
downloader = None

# Ensure downloads directory exists
DOWNLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'downloads')
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/download', methods=['POST'])
def start_download():
    global downloader

    data = request.get_json()
    if not data:
        return jsonify({'error': 'Invalid request body'}), 400

    urls = data.get('urls', [])
    max_workers = int(data.get('max_workers', 5))

    if not urls:
        return jsonify({'error': 'No URLs provided'}), 400

    if max_workers < 1 or max_workers > 20:
        max_workers = min(max(max_workers, 1), 20)

    # Create new downloader instance with user-selected max_workers
    downloader = MediaFireDownloader(max_workers=max_workers)

    download_ids = []
    errors = []
    for url in urls:
        url = url.strip()
        if not url:
            continue
        try:
            did = downloader.start_download(url, DOWNLOAD_DIR)
            download_ids.append(did)
        except Exception as e:
            errors.append({'url': url, 'error': str(e)})

    response = {
        'message': f'{len(download_ids)} download(s) started',
        'count': len(download_ids),
        'ids': download_ids,
    }
    if errors:
        response['errors'] = errors

    return jsonify(response)


@app.route('/api/status')
def get_status():
    if downloader is None:
        return jsonify({'downloads': {}})

    all_status = downloader.get_all_status()
    return jsonify({
        'downloads': {
            id_: {
                'filename': status.filename,
                'total_size': status.total_size,
                'downloaded': status.downloaded,
                'status': status.status.value,
                'speed': status.speed,
                'progress': status.progress,
                'eta': status.eta,
                'error': status.error
            }
            for id_, status in all_status.items()
        }
    })


@app.route('/api/download/control', methods=['POST'])
def control_download():
    if not downloader:
        return jsonify({'error': 'No active downloader'}), 400

    data = request.get_json()
    if not data:
        return jsonify({'error': 'Invalid request body'}), 400

    action = data.get('action')
    download_id = data.get('download_id')

    if not action or not download_id:
        return jsonify({'error': 'Missing action or download_id'}), 400

    success = False
    if action == 'pause':
        success = downloader.pause_download(download_id)
    elif action == 'resume':
        success = downloader.resume_download(download_id)
    elif action == 'cancel':
        success = downloader.cancel_download(download_id)
    else:
        return jsonify({'error': 'Invalid action'}), 400

    return jsonify({'success': success})


@app.route('/api/clear', methods=['POST'])
def clear_completed():
    if not downloader:
        return jsonify({'cleared': 0})

    count = downloader.clear_completed()
    return jsonify({'cleared': count})


if __name__ == '__main__':
    app.run(debug=True)
