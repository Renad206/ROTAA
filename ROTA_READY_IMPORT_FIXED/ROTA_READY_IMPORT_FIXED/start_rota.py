import threading,time,webbrowser,uvicorn,urllib.request
def open_when_ready():
    for _ in range(60):
        try:
            urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=1)
            webbrowser.open('http://127.0.0.1:8000');return
        except: time.sleep(1)
if __name__=='__main__':
    threading.Thread(target=open_when_ready,daemon=True).start()
    uvicorn.run('app:app',host='0.0.0.0',port=8000,reload=False)
