import os
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from flask import Flask, request, Response
import requests

FUNC_HOST   = os.environ.get('FUNC_HOST', 'http://localhost:7071')
AUTH_COOKIE = os.environ.get('AUTH_COOKIE', '')

app = Flask(__name__, static_folder='.', static_url_path='')

@app.route('/')
def index():
    return app.send_static_file('index.html')

@app.route('/.auth/<path:path>', methods=['GET','POST'])
def auth_proxy(path):
    url = f'https://job-eval.tools.sameen.dev/.auth/{path}'
    resp = requests.request(
        method=request.method,
        url=url,
        headers={k: v for k, v in request.headers if k != 'Host'},
        data=request.get_data(),
        params=request.args,
        cookies=request.cookies,
        verify=False,
        allow_redirects=False,
    )
    return Response(
        resp.content,
        status=resp.status_code,
        mimetype=resp.headers.get('Content-Type', 'application/json'),
    )

@app.route('/api/<path:path>', methods=['GET','POST','DELETE','PUT','PATCH'])
def proxy(path):
    url = f'{FUNC_HOST}/api/{path}'
    headers = {k: v for k, v in request.headers if k != 'Host'}
    if AUTH_COOKIE:
        headers['Cookie'] = AUTH_COOKIE
    resp = requests.request(
        method=request.method,
        url=url,
        headers=headers,
        data=request.get_data(),
        params=request.args,
        verify=False,
        allow_redirects=False,
    )
    return Response(
        resp.content,
        status=resp.status_code,
        mimetype=resp.headers.get('Content-Type', 'application/json'),
    )

if __name__ == '__main__':
    app.run(port=8080)