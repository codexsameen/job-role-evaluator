from flask import Flask, request, Response
import requests

app = Flask(__name__, static_folder='.', static_url_path='')

@app.route('/')
def index():
    return app.send_static_file('index.html')

@app.route('/api/<path:path>', methods=['GET','POST','DELETE','PUT','PATCH'])
def proxy(path):
    url = f'http://localhost:7071/api/{path}'
    resp = requests.request(
        method=request.method,
        url=url,
        headers={k: v for k, v in request.headers if k != 'Host'},
        data=request.get_data(),
        params=request.args,
    )
    return Response(
        resp.content,
        status=resp.status_code,
        mimetype=resp.headers.get('Content-Type', 'application/json'),
    )

if __name__ == '__main__':
    app.run(port=8080)