import urllib.request, json
import httpx, os

print(json.loads(urllib.request.urlopen('http://127.0.0.1:8205/health', timeout=5).read()))

r = httpx.post('http://127.0.0.1:8205/speak', json={'text': 'Hola, esto es una prueba de Kokoro.', 'voice': 'ef_dora'}, timeout=60)
print('status:', r.status_code, 'ct:', r.headers.get('content-type'), 'sr:', r.headers.get('X-Sample-Rate'))
data = r.content
print(f'recibidos {len(data)} bytes ({len(data)/2/24000:.2f}s @ 24kHz mono int16)')
out = os.path.join(os.environ['TEMP'], 'kokoro_test.pcm').replace('\\', '/')
with open(out, 'wb') as f:
    f.write(data)
print('guardado en', out)
