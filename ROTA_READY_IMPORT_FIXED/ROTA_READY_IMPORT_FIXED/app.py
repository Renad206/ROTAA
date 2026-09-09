from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from pathlib import Path
from urllib.parse import quote
from dotenv import load_dotenv
import sqlite3, os, time, random, hashlib, hmac, secrets, smtplib, ssl, shutil, json, re, requests
from email.message import EmailMessage
from datetime import datetime, timedelta
from openpyxl import load_workbook, Workbook
from scheduler import solve_schedule, DAYS_COUNT, PERIODS_COUNT

load_dotenv()
BASE=Path(__file__).resolve().parent
DATA_DIR=BASE/'data'; DATA_DIR.mkdir(exist_ok=True)
BACKUP_DIR=BASE/'backups'; BACKUP_DIR.mkdir(exist_ok=True)
DB=DATA_DIR/'rota.db'
DAYS=['الأحد','الاثنين','الثلاثاء','الأربعاء','الخميس']
PLANS={
 'basic': {'name':'Basic','price':39,'users':1,'features':['schedule','constraints','export']},
 'pro': {'name':'Pro','price':79,'users':5,'features':['schedule','constraints','export','absence','waiting','timetable_import','multiuser']},
 'premium': {'name':'Premium','price':129,'users':15,'features':['schedule','constraints','export','absence','waiting','timetable_import','multiuser','notifications','audit','versions']},
}
app=FastAPI(title='ROTA')
app.add_middleware(SessionMiddleware, secret_key=os.getenv('SESSION_SECRET','ROTA-change-me-in-production'), same_site='lax', https_only=False)
app.mount('/static',StaticFiles(directory=str(BASE/'static')),name='static')
templates=Jinja2Templates(directory=str(BASE/'templates'))

def con():
    c=sqlite3.connect(DB, timeout=30); c.row_factory=sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON'); c.execute('PRAGMA journal_mode=WAL'); return c

def cols(c,table): return [r['name'] for r in c.execute(f'PRAGMA table_info({table})')]
def ensure_col(c,table,name,ddl):
    if name not in cols(c,table): c.execute(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}')

def pwhash(password,salt=None):
    salt=salt or secrets.token_hex(16)
    h=hashlib.pbkdf2_hmac('sha256',password.encode(),salt.encode(),200000).hex()
    return salt+'$'+h

def pwcheck(password,stored):
    try:
        salt,h=stored.split('$',1); return hmac.compare_digest(pwhash(password,salt),stored)
    except Exception:return False

def init_db():
    c=con()
    # Existing single-school tables are migrated in-place.
    for t in ['teachers','classes','subjects','assignments','unavailable','schedule','absences','substitutions']:
        if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(t,)).fetchone(): ensure_col(c,t,'school_id','INTEGER NOT NULL DEFAULT 1')
    if 'teachers' in [r['name'] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]:
        ensure_col(c,'teachers','weekly_load','INTEGER NOT NULL DEFAULT 18'); ensure_col(c,'teachers','weekly_load_auto','INTEGER NOT NULL DEFAULT 1'); ensure_col(c,'teachers','notifications_enabled','INTEGER NOT NULL DEFAULT 1'); ensure_col(c,'teachers','email',"TEXT NOT NULL DEFAULT ''"); ensure_col(c,'teachers','subject_id','INTEGER')
    c.executescript('''
    CREATE TABLE IF NOT EXISTS schools(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,owner_name TEXT DEFAULT '',sender_phone TEXT DEFAULT '',sender_email TEXT DEFAULT '',plan TEXT NOT NULL DEFAULT 'pro',subscription_status TEXT NOT NULL DEFAULT 'trial',trial_ends_at INTEGER,subscription_ends_at INTEGER,auto_renew INTEGER NOT NULL DEFAULT 0,created_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,school_id INTEGER NOT NULL,name TEXT NOT NULL,username TEXT,email TEXT NOT NULL UNIQUE,password_hash TEXT NOT NULL,role TEXT NOT NULL DEFAULT 'owner',verified INTEGER NOT NULL DEFAULT 0,created_at INTEGER NOT NULL,FOREIGN KEY(school_id) REFERENCES schools(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS otp_codes(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,code_hash TEXT NOT NULL,expires_at INTEGER NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,used INTEGER NOT NULL DEFAULT 0,FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS school_constraints(id INTEGER PRIMARY KEY AUTOINCREMENT,school_id INTEGER NOT NULL,key TEXT NOT NULL,value TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,level TEXT NOT NULL DEFAULT 'soft',weight INTEGER NOT NULL DEFAULT 10,UNIQUE(school_id,key));
    CREATE TABLE IF NOT EXISTS schedule_versions(id INTEGER PRIMARY KEY AUTOINCREMENT,school_id INTEGER NOT NULL,name TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'draft',payload TEXT NOT NULL,created_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY AUTOINCREMENT,school_id INTEGER NOT NULL,provider TEXT NOT NULL DEFAULT 'moyasar',provider_payment_id TEXT,plan TEXT NOT NULL,amount INTEGER NOT NULL,status TEXT NOT NULL,created_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS audit_log(id INTEGER PRIMARY KEY AUTOINCREMENT,school_id INTEGER NOT NULL,user_id INTEGER,action TEXT NOT NULL,details TEXT DEFAULT '',created_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS import_jobs(id INTEGER PRIMARY KEY AUTOINCREMENT,school_id INTEGER NOT NULL,filename TEXT NOT NULL,status TEXT NOT NULL,report TEXT DEFAULT '',created_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY AUTOINCREMENT,school_id INTEGER NOT NULL,key TEXT NOT NULL,value TEXT NOT NULL DEFAULT '',UNIQUE(school_id,key));
    ''')
    ensure_col(c,'users','username','TEXT')
    # Existing accounts get a stable username without touching their email or data.
    existing_users=c.execute('SELECT id,email,username FROM users ORDER BY id').fetchall()
    used=set()
    for eu in existing_users:
        if eu['username']:
            used.add(eu['username'].lower()); continue
        candidate='admin' if eu['id']==1 else ('user'+str(eu['id']))
        n=2; base_candidate=candidate
        while candidate.lower() in used:
            candidate=f'{base_candidate}{n}'; n+=1
        c.execute('UPDATE users SET username=? WHERE id=?',(candidate,eu['id']))
        used.add(candidate.lower())
    try:
        c.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username_nocase ON users(username COLLATE NOCASE)')
    except sqlite3.IntegrityError:
        pass
    if not c.execute('SELECT id FROM schools WHERE id=1').fetchone():
        c.execute("INSERT INTO schools(id,name,owner_name,plan,subscription_status,trial_ends_at,created_at) VALUES(1,'مدرسة أمّي','الإدارة','premium','trial',?,?)",(int(time.time())+7*86400,int(time.time())))
    if not c.execute('SELECT id FROM users WHERE school_id=1').fetchone():
        c.execute("INSERT INTO users(school_id,name,username,email,password_hash,role,verified,created_at) VALUES(1,'الإدارة','admin','admin@rota.local',?,'owner',1,?)",(pwhash('Rota123!'),int(time.time())))
    defaults={
      'no_two_waiting_same_day':'1','max_waiting_week':'2','avoid_three_consecutive':'1','spread_lessons':'1','reduce_gaps':'1','balance_edge_periods':'1','math_no_period7':'1','other_period7_max':'2','art_digital_double':'1','activity_constraints':'1'
    }
    for k,v in defaults.items(): c.execute("INSERT OR IGNORE INTO school_constraints(school_id,key,value,enabled,level,weight) VALUES(1,?,?,1,'soft',10)",(k,v))
    c.commit(); c.close()
init_db()

def one(sql,args=()): c=con();r=c.execute(sql,args).fetchone();c.close();return r
def rows(sql,args=()): c=con();r=c.execute(sql,args).fetchall();c.close();return r
def exec1(sql,args=()): c=con();c.execute(sql,args);c.commit();c.close()
def now(): return int(time.time())
def user(request):
    uid=request.session.get('uid'); return one('SELECT * FROM users WHERE id=?',(uid,)) if uid else None
def school(request):
    u=user(request); return one('SELECT * FROM schools WHERE id=?',(u['school_id'],)) if u else None
def sid(request):
    u=user(request); return u['school_id'] if u else None
def require(request,roles=None):
    u=user(request)
    if not u:return None
    if roles and u['role'] not in roles:return None
    return u

def audit(request,action,details=''):
    u=user(request)
    if u: exec1('INSERT INTO audit_log(school_id,user_id,action,details,created_at) VALUES(?,?,?,?,?)',(u['school_id'],u['id'],action,details,now()))

def backup_db(label='auto'):
    stamp=datetime.now().strftime('%Y%m%d_%H%M%S'); dst=BACKUP_DIR/f'{label}_{stamp}.db'; shutil.copy2(DB,dst); return dst

def get_constraints(school_id):
    d={r['key']:dict(r) for r in rows('SELECT * FROM school_constraints WHERE school_id=?',(school_id,))}; return d

def has_feature(request,feature):
    s=school(request)
    if not s:return False
    plan=PLANS.get(s['plan'],PLANS['basic'])
    active=s['subscription_status'] in ('trial','active') and (not s['subscription_ends_at'] or s['subscription_ends_at']>now())
    return active and feature in plan['features']

def send_email_otp(to_email,code):
    host=os.getenv('SMTP_HOST',''); usern=os.getenv('SMTP_USER',''); password=os.getenv('SMTP_PASSWORD',''); port=int(os.getenv('SMTP_PORT','587'))
    if not host or not usern or not password:return {'ok':True,'mode':'dev'}
    msg=EmailMessage();msg['Subject']='رمز التحقق من ROTA';msg['From']=os.getenv('SMTP_FROM',usern);msg['To']=to_email
    msg.set_content(f'رمز التحقق الخاص بك في ROTA هو: {code}\nالرمز صالح لمدة 5 دقائق.')
    with smtplib.SMTP(host,port,timeout=20) as s:
        s.starttls(context=ssl.create_default_context());s.login(usern,password);s.send_message(msg)
    return {'ok':True,'mode':'smtp'}

def send_otp(user_id,email):
    code=f'{random.randint(0,999999):06d}'; h=hashlib.sha256(code.encode()).hexdigest(); exec1('INSERT INTO otp_codes(user_id,code_hash,expires_at) VALUES(?,?,?)',(user_id,h,now()+300)); r=send_email_otp(email,code); return code if r['mode']=='dev' else None

def trows(school_id): return rows('SELECT * FROM teachers WHERE school_id=? ORDER BY name',(school_id,))
def crows(school_id): return rows('SELECT * FROM classes WHERE school_id=? ORDER BY name',(school_id,))
def srows(school_id): return rows('SELECT * FROM subjects WHERE school_id=? ORDER BY name',(school_id,))
def arows(school_id): return rows('''SELECT a.*,t.name teacher_name,c.name class_name,s.name subject_name FROM assignments a JOIN teachers t ON t.id=a.teacher_id JOIN classes c ON c.id=a.class_id JOIN subjects s ON s.id=a.subject_id WHERE a.school_id=? ORDER BY t.name,c.name''',(school_id,))
def schedule_rows(school_id): return rows('''SELECT sc.id schedule_id,sc.day,sc.period,sc.locked,a.id assignment_id,t.id teacher_id,t.name teacher_name,t.phone,c.id class_id,c.name class_name,s.id subject_id,s.name subject_name FROM schedule sc JOIN assignments a ON a.id=sc.assignment_id JOIN teachers t ON t.id=a.teacher_id JOIN classes c ON c.id=a.class_id JOIN subjects s ON s.id=a.subject_id WHERE sc.school_id=? ORDER BY sc.day,sc.period,t.name''',(school_id,))

@app.get('/health')
def health(): return {'ok':True,'app':'ROTA'}
@app.get('/login',response_class=HTMLResponse)
def login_page(request:Request,error:str=''): return templates.TemplateResponse(request=request,name='login.html',context={'error':error})
@app.post('/login')
def login(request:Request,username:str=Form(...),password:str=Form(...)):
    u=one('SELECT * FROM users WHERE lower(username)=lower(?)',(username.strip(),))
    if not u or not pwcheck(password,u['password_hash']):
        return RedirectResponse('/login?error='+quote('اسم المستخدم أو كلمة المرور غير صحيحة'),303)
    if not u['verified']:
        dev=send_otp(u['id'],u['email']);request.session['verify_uid']=u['id'];request.session['verify_mode']='signup'
        if dev:request.session['dev_otp']=dev
        return RedirectResponse('/verify',303)
    request.session['uid']=u['id'];return RedirectResponse('/',303)

@app.get('/register',response_class=HTMLResponse)
def register_page(request:Request,error:str=''): return templates.TemplateResponse(request=request,name='register.html',context={'error':error})
@app.post('/register')
def register(request:Request,name:str=Form(...),username:str=Form(...),email:str=Form(...),password:str=Form(...),school_name:str=Form(...),sender_phone:str=Form('')):
    username=username.strip()
    if not re.fullmatch(r'[A-Za-z0-9_.-]{3,30}',username):
        return RedirectResponse('/register?error='+quote('اسم المستخدم يجب أن يكون 3-30 حرفًا إنجليزيًا أو رقمًا ويمكن استخدام _ . -'),303)
    if one('SELECT id FROM users WHERE lower(username)=lower(?)',(username,)):
        return RedirectResponse('/register?error='+quote('اسم المستخدم مستخدم مسبقًا'),303)
    if one('SELECT id FROM users WHERE lower(email)=lower(?)',(email.strip(),)):
        return RedirectResponse('/register?error='+quote('الإيميل مسجل مسبقًا'),303)
    c=con();cur=c.execute("INSERT INTO schools(name,owner_name,sender_phone,plan,subscription_status,trial_ends_at,created_at) VALUES(?,?,?,?,?,?,?)",(school_name.strip(),name.strip(),sender_phone.strip(),'pro','trial',now()+7*86400,now()));school_id=cur.lastrowid
    cur=c.execute("INSERT INTO users(school_id,name,username,email,password_hash,role,verified,created_at) VALUES(?,?,?,?,?,?,?,?)",(school_id,name.strip(),username,email.strip().lower(),pwhash(password),'owner',0,now()));uid=cur.lastrowid
    for cn in ['1/1','1/2','1/3','2/1','2/2','2/3','3/1','3/2','3/3']: c.execute('INSERT INTO classes(name,school_id) VALUES(?,?)',(cn,school_id))
    for sn in ['اجتماعيات','أسرية','إسلاميات','إنجليزي','بدنية','تفكير ناقد','رقمية','رياضيات','علوم','فنية','لغتي','نشاط']: c.execute('INSERT INTO subjects(name,school_id) VALUES(?,?)',(sn,school_id))
    c.commit();c.close()
    dev=send_otp(uid,email.strip().lower());request.session['verify_uid']=uid;request.session['verify_mode']='signup'
    if dev: request.session['dev_otp']=dev
    return RedirectResponse('/verify',303)

@app.get('/verify',response_class=HTMLResponse)
def verify_page(request:Request,error:str=''):
    return templates.TemplateResponse(request=request,name='verify.html',context={'error':error,'dev_otp':request.session.get('dev_otp'),'mode':request.session.get('verify_mode','signup')})
@app.post('/verify')
def verify(request:Request,otp:str=Form(...)):
    uid=request.session.get('verify_uid')
    if not uid:return RedirectResponse('/login',303)
    rec=one('SELECT * FROM otp_codes WHERE user_id=? AND used=0 ORDER BY id DESC LIMIT 1',(uid,))
    if not rec or rec['expires_at']<now():return RedirectResponse('/verify?error='+quote('انتهت صلاحية الرمز'),303)
    if rec['attempts']>=5:return RedirectResponse('/verify?error='+quote('تم تجاوز عدد المحاولات'),303)
    if hashlib.sha256(otp.strip().encode()).hexdigest()!=rec['code_hash']:
        exec1('UPDATE otp_codes SET attempts=attempts+1 WHERE id=?',(rec['id'],));return RedirectResponse('/verify?error='+quote('الرمز غير صحيح'),303)
    exec1('UPDATE otp_codes SET used=1 WHERE id=?',(rec['id'],))
    mode=request.session.get('verify_mode','signup')
    if mode=='reset':
        request.session['reset_uid']=uid;request.session.pop('dev_otp',None)
        return RedirectResponse('/reset-password',303)
    exec1('UPDATE users SET verified=1 WHERE id=?',(uid,));request.session['uid']=uid
    request.session.pop('dev_otp',None);request.session.pop('verify_mode',None)
    return RedirectResponse('/',303)

@app.post('/verify/resend')
def verify_resend(request:Request):
    uid=request.session.get('verify_uid')
    if not uid:return RedirectResponse('/login',303)
    u=one('SELECT * FROM users WHERE id=?',(uid,))
    dev=send_otp(uid,u['email'])
    if dev:request.session['dev_otp']=dev
    return RedirectResponse('/verify',303)

@app.get('/forgot-password',response_class=HTMLResponse)
def forgot_page(request:Request,error:str=''):
    return templates.TemplateResponse(request=request,name='forgot.html',context={'error':error})
@app.post('/forgot-password')
def forgot(request:Request,identifier:str=Form(...)):
    ident=identifier.strip()
    u=one('SELECT * FROM users WHERE lower(username)=lower(?) OR lower(email)=lower(?)',(ident,ident))
    # Keep response neutral for account privacy.
    if not u:return RedirectResponse('/forgot-password?error='+quote('تعذر العثور على الحساب. تأكدي من اسم المستخدم أو البريد.'),303)
    dev=send_otp(u['id'],u['email']);request.session['verify_uid']=u['id'];request.session['verify_mode']='reset'
    if dev:request.session['dev_otp']=dev
    return RedirectResponse('/verify',303)

@app.get('/reset-password',response_class=HTMLResponse)
def reset_password_page(request:Request,error:str=''):
    if not request.session.get('reset_uid'):return RedirectResponse('/login',303)
    return templates.TemplateResponse(request=request,name='reset_password.html',context={'error':error})
@app.post('/reset-password')
def reset_password(request:Request,password:str=Form(...),confirm_password:str=Form(...)):
    uid=request.session.get('reset_uid')
    if not uid:return RedirectResponse('/login',303)
    if len(password)<8:return RedirectResponse('/reset-password?error='+quote('كلمة المرور يجب أن تكون 8 أحرف على الأقل'),303)
    if password!=confirm_password:return RedirectResponse('/reset-password?error='+quote('كلمتا المرور غير متطابقتين'),303)
    exec1('UPDATE users SET password_hash=? WHERE id=?',(pwhash(password),uid));request.session.clear()
    return RedirectResponse('/login?error='+quote('تم تغيير كلمة المرور، سجلي الدخول بكلمة المرور الجديدة'),303)

@app.get('/logout')
def logout(request:Request):request.session.clear();return RedirectResponse('/login',303)

@app.get('/',response_class=HTMLResponse)
def dashboard(request:Request):
    u=require(request)
    if not u:return RedirectResponse('/login',303)
    s=school(request);school_id=u['school_id']; stats={'teachers':one('SELECT COUNT(*) n FROM teachers WHERE school_id=?',(school_id,))['n'],'classes':one('SELECT COUNT(*) n FROM classes WHERE school_id=?',(school_id,))['n'],'subjects':one('SELECT COUNT(*) n FROM subjects WHERE school_id=?',(school_id,))['n'],'pending':one("SELECT COUNT(*) n FROM substitutions WHERE school_id=? AND status='pending'",(school_id,))['n']}
    return templates.TemplateResponse(request=request,name='dashboard.html',context={'u':u,'school':s,'stats':stats,'teachers':trows(school_id),'classes':crows(school_id),'subjects':srows(school_id),'assignments':arows(school_id),'plans':PLANS})

@app.post('/teacher')
def add_teacher(request:Request,name:str=Form(...),phone:str=Form(''),email:str=Form(''),weekly_load:int=Form(0),subject_id:str=Form('')):
    u=require(request,['owner','admin','editor']);
    if not u:return RedirectResponse('/login',303)
    exec1('INSERT INTO teachers(name,phone,email,weekly_load,weekly_load_auto,max_daily,max_consecutive,subject_id,school_id) VALUES(?,?,?,?,?,?,?,?,?)',(name.strip(),phone.strip(),email.strip(),max(0,weekly_load),0 if weekly_load else 1,5,3,int(subject_id) if subject_id else None,u['school_id']));audit(request,'add_teacher',name);return RedirectResponse('/',303)
@app.post('/teacher/edit/{id}')
def edit_teacher(request:Request,id:int,name:str=Form(...),phone:str=Form(''),email:str=Form(''),weekly_load:int=Form(0),subject_id:str=Form('')):
    u=require(request,['owner','admin','editor']);
    if not u:return RedirectResponse('/login',303)
    exec1('UPDATE teachers SET name=?,phone=?,email=?,weekly_load=?,weekly_load_auto=0,subject_id=? WHERE id=? AND school_id=?',(name.strip(),phone.strip(),email.strip(),max(0,weekly_load),int(subject_id) if subject_id else None,id,u['school_id']));return RedirectResponse('/',303)
@app.post('/teacher/delete/{id}')
def del_teacher(request:Request,id:int):
    u=require(request,['owner','admin']);
    if u:exec1('DELETE FROM teachers WHERE id=? AND school_id=?',(id,u['school_id']))
    return RedirectResponse('/',303)
@app.post('/assignment')
def add_assignment(request:Request,teacher_id:int=Form(...),subject_id:int=Form(...),class_ids:list[int]=Form(...),weekly_periods:int=Form(...)):
    u=require(request,['owner','admin','editor']);
    if not u:return RedirectResponse('/login',303)
    c=con()
    for cid in class_ids:
        ex=c.execute('SELECT id FROM assignments WHERE school_id=? AND teacher_id=? AND class_id=? AND subject_id=?',(u['school_id'],teacher_id,cid,subject_id)).fetchone()
        if ex:c.execute('UPDATE assignments SET weekly_periods=? WHERE id=?',(weekly_periods,ex['id']))
        else:c.execute('INSERT INTO assignments(teacher_id,class_id,subject_id,weekly_periods,school_id) VALUES(?,?,?,?,?)',(teacher_id,cid,subject_id,weekly_periods,u['school_id']))
    c.commit();c.close();return RedirectResponse('/',303)
@app.post('/assignment/delete/{id}')
def del_assignment(request:Request,id:int):
    u=require(request,['owner','admin','editor']);
    if u:exec1('DELETE FROM assignments WHERE id=? AND school_id=?',(id,u['school_id']))
    return RedirectResponse('/',303)

@app.get('/constraints',response_class=HTMLResponse)
def constraints(request:Request):
    u=require(request)
    if not u:return RedirectResponse('/login',303)
    return templates.TemplateResponse(request=request,name='constraints.html',context={'u':u,'school':school(request),'cfg':get_constraints(u['school_id']),'teachers':trows(u['school_id']),'specific':rows('SELECT u.*,t.name teacher_name FROM unavailable u JOIN teachers t ON t.id=u.teacher_id WHERE u.school_id=? ORDER BY t.name,u.day,u.period',(u['school_id'],)),'days':DAYS})
@app.post('/constraints/save')
def save_constraints(request:Request,no_two_waiting_same_day:str=Form('0'),max_waiting_week:int=Form(2),avoid_three_consecutive:str=Form('0'),spread_lessons:str=Form('0'),reduce_gaps:str=Form('0'),balance_edge_periods:str=Form('0'),math_no_period7:str=Form('0'),other_period7_max:int=Form(2),art_digital_double:str=Form('0'),activity_constraints:str=Form('0')):
    u=require(request,['owner','admin','editor']);
    if not u:return RedirectResponse('/login',303)
    vals=locals().copy();keys=['no_two_waiting_same_day','max_waiting_week','avoid_three_consecutive','spread_lessons','reduce_gaps','balance_edge_periods','math_no_period7','other_period7_max','art_digital_double','activity_constraints']
    c=con()
    for k in keys:
        v=str(vals[k]); enabled=1 if (k in ['max_waiting_week','other_period7_max'] or v!='0') else 0
        c.execute("INSERT INTO school_constraints(school_id,key,value,enabled,level,weight) VALUES(?,?,?,?,?,10) ON CONFLICT(school_id,key) DO UPDATE SET value=excluded.value,enabled=excluded.enabled",(u['school_id'],k,v,enabled,'hard' if k in ['math_no_period7','art_digital_double'] else 'soft'))
    c.commit();c.close();audit(request,'save_constraints');return RedirectResponse('/constraints?message='+quote('تم حفظ القيود'),303)
@app.post('/constraint/specific')
def specific_constraint(request:Request,teacher_id:int=Form(...),day:int=Form(...),period:int=Form(...),level:str=Form('hard'),weight:int=Form(10)):
    u=require(request,['owner','admin','editor']);
    if u:exec1('INSERT INTO unavailable(teacher_id,day,period,level,weight,school_id) VALUES(?,?,?,?,?,?)',(teacher_id,day,period,level,max(1,min(100,weight)),u['school_id']))
    return RedirectResponse('/constraints',303)
@app.post('/constraint/delete/{id}')
def del_constraint(request:Request,id:int):
    u=require(request,['owner','admin','editor']);
    if u:exec1('DELETE FROM unavailable WHERE id=? AND school_id=?',(id,u['school_id']))
    return RedirectResponse('/constraints',303)

@app.post('/generate')
def generate(request:Request):
    u=require(request,['owner','admin','editor'])
    if not u:return RedirectResponse('/login',303)
    school_id=u['school_id'];cfg=get_constraints(school_id); backup_db('before_generate')
    result=solve_schedule(trows(school_id),crows(school_id),rows('SELECT a.*,s.name subject_name FROM assignments a JOIN subjects s ON s.id=a.subject_id WHERE a.school_id=?',(school_id,)),rows('SELECT * FROM unavailable WHERE school_id=?',(school_id,)),rows('SELECT assignment_id,day,period FROM schedule WHERE school_id=? AND locked=1',(school_id,)),cfg)
    if not result['ok']:return RedirectResponse('/schedule?message='+quote(' | '.join(result['errors'])),303)
    c=con();c.execute('DELETE FROM schedule WHERE school_id=? AND locked=0',(school_id,));locked={(r['assignment_id'],r['day'],r['period']) for r in c.execute('SELECT assignment_id,day,period FROM schedule WHERE school_id=? AND locked=1',(school_id,))}
    for x in result['lessons']:
        if (x['assignment_id'],x['day'],x['period']) not in locked:c.execute('INSERT INTO schedule(assignment_id,day,period,locked,school_id) VALUES(?,?,?,?,?)',(x['assignment_id'],x['day'],x['period'],0,school_id))
    c.commit();c.close();audit(request,'generate_schedule');save_version(school_id,'إنشاء تلقائي','draft');return RedirectResponse('/schedule?message='+quote('تم إنشاء الجدول'),303)

def save_version(school_id,name,status='draft'):
    data=[dict(r) for r in schedule_rows(school_id)];exec1('INSERT INTO schedule_versions(school_id,name,status,payload,created_at) VALUES(?,?,?,?,?)',(school_id,name,status,json.dumps(data,ensure_ascii=False),now()))

@app.get('/schedule',response_class=HTMLResponse)
def schedule_page(request:Request,message:str=''):
    u=require(request)
    if not u:return RedirectResponse('/login',303)
    school_id=u['school_id']; data=schedule_rows(school_id); teacher_board={}
    teachers=[dict(t) for t in trows(school_id)]
    for t in teachers:
        teacher_board[t['id']]={(d,p):next((dict(x) for x in data if x['teacher_id']==t['id'] and x['day']==d and x['period']==p),None) for d in range(5) for p in range(1,8)}
        t['distributed']=sum(1 for x in data if x['teacher_id']==t['id'])
    return templates.TemplateResponse(request=request,name='schedule.html',context={'u':u,'school':school(request),'teachers':teachers,'classes':crows(school_id),'subjects':srows(school_id),'teacher_board':teacher_board,'days':DAYS,'message':message})
@app.post('/schedule/add')
def schedule_add(request:Request,teacher_id:int=Form(...),class_id:int=Form(...),subject_id:int=Form(...),day:int=Form(...),period:int=Form(...)):
    u=require(request,['owner','admin','editor']);
    if not u:return RedirectResponse('/login',303)
    school_id=u['school_id']
    busy=one('''SELECT sc.id FROM schedule sc JOIN assignments a ON a.id=sc.assignment_id WHERE sc.school_id=? AND sc.day=? AND sc.period=? AND (a.teacher_id=? OR a.class_id=?)''',(school_id,day,period,teacher_id,class_id))
    if busy:return RedirectResponse('/schedule?message='+quote('يوجد تعارض حقيقي للمعلمة أو الفصل'),303)
    a=one('SELECT id FROM assignments WHERE school_id=? AND teacher_id=? AND class_id=? AND subject_id=?',(school_id,teacher_id,class_id,subject_id))
    if not a:
        c=con();cur=c.execute('INSERT INTO assignments(teacher_id,class_id,subject_id,weekly_periods,school_id) VALUES(?,?,?,?,?)',(teacher_id,class_id,subject_id,1,school_id));aid=cur.lastrowid;c.commit();c.close()
    else:aid=a['id']
    exec1('INSERT INTO schedule(assignment_id,day,period,locked,school_id) VALUES(?,?,?,?,?)',(aid,day,period,1,school_id));return RedirectResponse('/schedule',303)

@app.post('/schedule/add-cell')
def schedule_add_cell(request:Request,teacher_id:int=Form(...),class_id:int=Form(...),subject_id:int=Form(...),day:int=Form(...),period:int=Form(...)):
    return schedule_add(request,teacher_id,class_id,subject_id,day,period)

@app.post('/lesson/lock/{id}')
def lesson_lock(request:Request,id:int):
    u=require(request,['owner','admin','editor'])
    if not u:return RedirectResponse('/login',303)
    r=one('SELECT locked FROM schedule WHERE id=? AND school_id=?',(id,u['school_id']))
    if r:exec1('UPDATE schedule SET locked=? WHERE id=? AND school_id=?',(0 if r['locked'] else 1,id,u['school_id']))
    return RedirectResponse('/schedule',303)

@app.post('/schedule/delete')
def schedule_delete_all(request:Request):
    u=require(request,['owner','admin'])
    if not u:return RedirectResponse('/login',303)
    backup_db('before_schedule_delete');exec1('DELETE FROM schedule WHERE school_id=?',(u['school_id'],));audit(request,'delete_schedule')
    return RedirectResponse('/schedule?message='+quote('تم حذف الجدول'),303)

@app.post('/schedule/rebuild')
def schedule_rebuild(request:Request):
    u=require(request,['owner','admin','editor'])
    if not u:return RedirectResponse('/login',303)
    backup_db('before_schedule_rebuild');exec1('DELETE FROM schedule WHERE school_id=?',(u['school_id'],))
    return generate(request)

@app.post('/lesson/delete/{id}')
def lesson_delete(request:Request,id:int):
    u=require(request,['owner','admin','editor']);
    if u:exec1('DELETE FROM schedule WHERE id=? AND school_id=?',(id,u['school_id']))
    return RedirectResponse('/schedule',303)
@app.post('/schedule/approve')
def approve_schedule(request:Request):
    u=require(request,['owner','admin']);
    if not u:return RedirectResponse('/login',303)
    save_version(u['school_id'],'الجدول المعتمد','approved');audit(request,'approve_schedule');return RedirectResponse('/schedule?message='+quote('تم اعتماد نسخة من الجدول'),303)

@app.get('/absence',response_class=HTMLResponse)
def absence_page(request:Request,day:int=0,message:str=''):
    u=require(request)
    if not u:return RedirectResponse('/login',303)
    if not has_feature(request,'absence'):return RedirectResponse('/subscription',303)
    school_id=u['school_id']; rs=rows('''SELECT sub.id substitution_id,sub.status,sub.notified,sub.substitute_teacher_id,abs.day,sc.period,c.name class_name,s.name subject_name,t.id absent_teacher_id,t.name absent_teacher_name FROM substitutions sub JOIN absences abs ON abs.id=sub.absence_id JOIN schedule sc ON sc.id=sub.schedule_id JOIN assignments a ON a.id=sc.assignment_id JOIN teachers t ON t.id=a.teacher_id JOIN classes c ON c.id=a.class_id JOIN subjects s ON s.id=a.subject_id WHERE sub.school_id=? AND abs.day=? ORDER BY sc.period''',(school_id,day))
    enriched=[]
    for r in rs:
        item=dict(r);cands=[]
        for t in trows(school_id):
            if t['id']==r['absent_teacher_id']:continue
            if teacher_free(school_id,t['id'],day,r['period']):cands.append({'id':t['id'],'name':t['name'],'phone':t['phone'],'score':waiting_score(school_id,t,day,r['period'])})
        cands.sort(key=lambda x:x['score'],reverse=True);item['candidates']=cands[:7];item['selected_teacher_name']=next((x['name'] for x in cands if x['id']==r['substitute_teacher_id']),'');enriched.append(item)
    return templates.TemplateResponse(request=request,name='absence.html',context={'u':u,'school':school(request),'teachers':trows(school_id),'rows':enriched,'days':DAYS,'day':day,'message':message,'sender_phone':school(request)['sender_phone']})

def teacher_free(school_id,tid,day,period):
    if one("SELECT id FROM unavailable WHERE school_id=? AND teacher_id=? AND day=? AND period=? AND level='hard'",(school_id,tid,day,period)):return False
    return not one('''SELECT sc.id FROM schedule sc JOIN assignments a ON a.id=sc.assignment_id WHERE sc.school_id=? AND a.teacher_id=? AND sc.day=? AND sc.period=?''',(school_id,tid,day,period))
def waiting_score(school_id,t,day,period):
    daily=one("SELECT COUNT(*) n FROM substitutions sub JOIN absences a ON a.id=sub.absence_id WHERE sub.school_id=? AND sub.substitute_teacher_id=? AND sub.status='assigned' AND a.day=?",(school_id,t['id'],day))['n']
    weekly=one("SELECT COUNT(*) n FROM substitutions WHERE school_id=? AND substitute_teacher_id=? AND status='assigned'",(school_id,t['id']))['n']
    load=one('''SELECT COUNT(*) n FROM schedule sc JOIN assignments a ON a.id=sc.assignment_id WHERE sc.school_id=? AND a.teacher_id=? AND sc.day=?''',(school_id,t['id'],day))['n']
    cfg=get_constraints(school_id); maxw=int(cfg.get('max_waiting_week',{'value':'2'})['value'])
    if cfg.get('no_two_waiting_same_day',{}).get('enabled') and daily>=1:return -999
    if weekly>=maxw:return -500
    return 100-weekly*20-daily*25-load*8-(5 if period==7 else 0)
@app.post('/absence')
def add_absence(request:Request,teacher_id:int=Form(...),day:int=Form(...),note:str=Form('')):
    u=require(request,['owner','admin','editor']);
    if not u:return RedirectResponse('/login',303)
    c=con();cur=c.execute('INSERT INTO absences(teacher_id,day,note,school_id) VALUES(?,?,?,?)',(teacher_id,day,note,u['school_id']));aid=cur.lastrowid
    for l in c.execute('''SELECT sc.id FROM schedule sc JOIN assignments a ON a.id=sc.assignment_id WHERE sc.school_id=? AND a.teacher_id=? AND sc.day=?''',(u['school_id'],teacher_id,day)):c.execute("INSERT INTO substitutions(absence_id,schedule_id,status,school_id) VALUES(?,?,?,?)",(aid,l['id'],'pending',u['school_id']))
    c.commit();c.close();return RedirectResponse(f'/absence?day={day}',303)
@app.post('/substitution/assign/{id}')
def assign_waiting(request:Request,id:int,teacher_id:int=Form(...)):
    u=require(request,['owner','admin','editor']);
    if u:exec1("UPDATE substitutions SET substitute_teacher_id=?,status='assigned' WHERE id=? AND school_id=?",(teacher_id,id,u['school_id']))
    return RedirectResponse('/absence',303)
@app.get('/substitution/message/{id}')
def waiting_message(request:Request,id:int):
    u=require(request)
    if not u:return RedirectResponse('/login',303)
    r=one('''SELECT sub.id,abs.day,sc.period,c.name class_name,s.name subject_name,t.name substitute_name,t.phone,orig.name absent_name FROM substitutions sub JOIN absences abs ON abs.id=sub.absence_id JOIN schedule sc ON sc.id=sub.schedule_id JOIN assignments a ON a.id=sc.assignment_id JOIN classes c ON c.id=a.class_id JOIN subjects s ON s.id=a.subject_id JOIN teachers orig ON orig.id=a.teacher_id JOIN teachers t ON t.id=sub.substitute_teacher_id WHERE sub.id=? AND sub.school_id=?''',(id,u['school_id']))
    if not r:return RedirectResponse('/absence',303)
    msg=f"السلام عليكم أ. {r['substitute_name']}،\nتم إسناد حصة انتظار لك يوم {DAYS[r['day']]}، الحصة {r['period']}، الفصل {r['class_name']} لتغطية غياب أ. {r['absent_name']}.\nROTA"
    phone=re.sub(r'\D','',r['phone'] or '')
    if phone.startswith('05'):phone='966'+phone[1:]
    exec1('UPDATE substitutions SET notified=1 WHERE id=?',(id,));return RedirectResponse('https://wa.me/'+phone+'?text='+quote(msg),302)

@app.get('/import-timetable',response_class=HTMLResponse)
def import_page(request:Request,message:str=''):
    u=require(request)
    if not u:return RedirectResponse('/login',303)
    if not has_feature(request,'timetable_import'):return RedirectResponse('/subscription',303)
    return templates.TemplateResponse(request=request,name='import.html',context={'u':u,'school':school(request),'message':message})

def _norm_text(value):
    s=str(value or '').strip().lower()
    s=re.sub(r'[أإآٱ]', 'ا', s)
    s=s.replace('ى','ي').replace('ة','ه').replace('ؤ','و').replace('ئ','ي')
    s=s.replace('عبدهللا','عبدالله')
    s=re.sub(r'\s+', ' ', s)
    return s

def _header_index(header, aliases):
    idx={}
    for key,opts in aliases.items():
        for i,h in enumerate(header):
            nh=_norm_text(h)
            if any(_norm_text(o) in nh for o in opts):
                idx[key]=i; break
    return idx

def parse_excel(path):
    wb=load_workbook(path,data_only=True); lessons=[]; notes=[]
    aliases={
        'teacher':['teacher','المعلمة','المعلم','اسم المعلمة','اسم المعلم','اسم المدرس','المدرس','الاسم'],
        'class':['class','الفصل','الصف','الشعبة','الصف والفصل','الصف/الفصل'],
        'subject':['subject','المادة','المقرر','اسم المادة'],
        'day':['day','اليوم','اسم اليوم'],
        'period':['period','الحصة','رقم الحصة','الحصه']
    }
    for ws in wb.worksheets:
        vals=list(ws.iter_rows(values_only=True))
        if not vals: continue
        for header_row in range(min(10,len(vals))):
            header=[str(x or '').strip() for x in vals[header_row]]
            idx=_header_index(header,aliases)
            if not {'teacher','class','day','period'}.issubset(idx): continue
            for row in vals[header_row+1:]:
                try:
                    teacher=str(row[idx['teacher']] or '').strip(); cl=str(row[idx['class']] or '').strip()
                    if not teacher or not cl: continue
                    d=str(row[idx['day']] or '').strip(); nd=_norm_text(d); day=None
                    for di,dn in enumerate(DAYS):
                        if _norm_text(dn)==nd: day=di; break
                    if day is None:
                        iv=int(float(d)); day=iv if 0<=iv<=4 else iv-1
                    period=int(float(row[idx['period']]))
                    if not 1<=period<=7: continue
                    subject=str(row[idx['subject']] or '').strip() if 'subject' in idx else ''
                    lessons.append({'teacher':teacher,'class':cl,'subject':subject,'day':day,'period':period})
                except Exception: continue
            if lessons:
                if 'subject' not in idx: notes.append('تمت قراءة الملف بدون عمود المادة؛ سيحاول ROTA استنتاج المادة من الإسنادات الموجودة.')
                return lessons,notes
    for ws in wb.worksheets:
        vals=list(ws.iter_rows(values_only=True))
        if len(vals)<2: continue
        for hr in range(min(5,len(vals))):
            header=[str(x or '').strip() for x in vals[hr]]; positions=[]; teacher_col=0
            for i,h in enumerate(header):
                nh=_norm_text(h)
                if any(x in nh for x in ['المعلم','المعلمة','teacher','الاسم']): teacher_col=i
                m=re.search(r'(الأحد|الاحد|الاثنين|الثلاثاء|الأربعاء|الاربعاء|الخميس).*?(\d)',h)
                if m:
                    dn=_norm_text(m.group(1)); di=next((j for j,x in enumerate(DAYS) if _norm_text(x)==dn),None)
                    if di is not None: positions.append((i,di,int(m.group(2))))
            if not positions: continue
            for row in vals[hr+1:]:
                teacher=str(row[teacher_col] or '').strip()
                if not teacher: continue
                for i,d,p in positions:
                    if i>=len(row): continue
                    cell=str(row[i] or '').strip()
                    if not cell: continue
                    parts=re.split(r'\s*[-|–]\s*',cell,maxsplit=1)
                    cl,sub=(parts[0],parts[1]) if len(parts)==2 else (cell,'')
                    lessons.append({'teacher':teacher,'class':cl.strip(),'subject':sub.strip(),'day':d,'period':p})
            if lessons:return lessons,notes
    notes.append('لم أتعرف على تنسيق الملف. تأكدي أن الملف يحتوي على اسم المعلمة والفصل واليوم والحصة، أو جدول أسبوعي واضح. عمود المادة اختياري إذا كانت الإسنادات موجودة في ROTA.')
    return lessons,notes

def _best_row(c, table, school_id, value, column='name'):
    value=str(value or '').strip()
    if not value:return None
    exact=c.execute(f'SELECT * FROM {table} WHERE school_id=? AND trim({column})=trim(?)',(school_id,value)).fetchone()
    if exact:return exact
    from difflib import SequenceMatcher
    nv=_norm_text(value); best=None; score=0
    for r in c.execute(f'SELECT * FROM {table} WHERE school_id=?',(school_id,)).fetchall():
        sc=SequenceMatcher(None,nv,_norm_text(r[column])).ratio()
        if sc>score:score=sc;best=r
    return best if score>=0.64 else None

@app.post('/import-timetable')
async def import_excel(request:Request,file:UploadFile=File(...)):
    u=require(request,['owner','admin','editor'])
    if not u:return RedirectResponse('/login',303)
    if not file.filename.lower().endswith(('.xlsx','.xlsm')):return RedirectResponse('/import-timetable?message='+quote('ارفعي ملف Excel بصيغة xlsx'),303)
    temp=DATA_DIR/'import.xlsx';temp.write_bytes(await file.read());lessons,notes=parse_excel(temp)
    if not lessons:return RedirectResponse('/import-timetable?message='+quote(' | '.join(notes)),303)
    school_id=u['school_id']; c=con();errors=[];added=0;inferred=0;unknown_subject=0
    backup_db('before_import');c.execute('DELETE FROM substitutions WHERE school_id=?',(school_id,));c.execute('DELETE FROM absences WHERE school_id=?',(school_id,));c.execute('DELETE FROM schedule WHERE school_id=?',(school_id,))
    neutral=c.execute("SELECT id FROM subjects WHERE school_id=? AND name='غير محددة'",(school_id,)).fetchone()
    if not neutral:
        cur=c.execute("INSERT INTO subjects(name,school_id) VALUES('غير محددة',?)",(school_id,));neutral_id=cur.lastrowid
    else:neutral_id=neutral['id']
    for x in lessons:
        t=_best_row(c,'teachers',school_id,x['teacher']); cl=_best_row(c,'classes',school_id,x['class'])
        if not t or not cl:
            errors.append(f"لم أجد المعلمة أو الفصل: {x['teacher']} / {x['class']}");continue
        su=_best_row(c,'subjects',school_id,x.get('subject','')) if x.get('subject') else None
        if not su:
            opts=c.execute('SELECT s.id,s.name FROM assignments a JOIN subjects s ON s.id=a.subject_id WHERE a.school_id=? AND a.teacher_id=? AND a.class_id=?',(school_id,t['id'],cl['id'])).fetchall()
            if len(opts)==1: su=opts[0]; inferred+=1
            else: su={'id':neutral_id,'name':'غير محددة'}; unknown_subject+=1
        a=c.execute('SELECT id FROM assignments WHERE school_id=? AND teacher_id=? AND class_id=? AND subject_id=?',(school_id,t['id'],cl['id'],su['id'])).fetchone()
        if not a:
            cur=c.execute('INSERT INTO assignments(teacher_id,class_id,subject_id,weekly_periods,school_id) VALUES(?,?,?,?,?)',(t['id'],cl['id'],su['id'],1,school_id));aid=cur.lastrowid
        else:aid=a['id']
        conflict=c.execute('SELECT sc.id FROM schedule sc JOIN assignments a ON a.id=sc.assignment_id WHERE sc.school_id=? AND sc.day=? AND sc.period=? AND (a.teacher_id=? OR a.class_id=?)',(school_id,x['day'],x['period'],t['id'],cl['id'])).fetchone()
        if conflict:errors.append(f"تعارض: {t['name']} يوم {DAYS[x['day']]} حصة {x['period']}");continue
        c.execute('INSERT INTO schedule(assignment_id,day,period,locked,school_id) VALUES(?,?,?,?,?)',(aid,x['day'],x['period'],1,school_id));added+=1
    report={'added':added,'errors':errors,'inferred_subjects':inferred,'unknown_subjects':unknown_subject,'notes':notes}
    c.execute('INSERT INTO import_jobs(school_id,filename,status,report,created_at) VALUES(?,?,?,?,?)',(school_id,file.filename,'done',json.dumps(report,ensure_ascii=False),now()));c.commit();c.close();save_version(school_id,'استيراد Timetable','draft')
    msg=f'تم استيراد {added} حصة'
    if inferred:msg+=f'، واستنتاج المادة تلقائيًا لـ {inferred} حصة'
    if unknown_subject:msg+=f'، و{unknown_subject} حصة تحتاج مراجعة المادة'
    if errors:msg+=f'، مع {len(errors)} ملاحظة'
    return RedirectResponse('/schedule?message='+quote(msg),303)

@app.get('/export.xlsx')
def export_excel(request:Request):
    u=require(request)
    if not u:return RedirectResponse('/login',303)
    wb=Workbook();ws=wb.active;ws.title='ROTA';ws.append(['المعلمة','الفصل','المادة','اليوم','الحصة'])
    for r in schedule_rows(u['school_id']):ws.append([r['teacher_name'],r['class_name'],r['subject_name'],DAYS[r['day']],r['period']])
    out=DATA_DIR/'ROTA_export.xlsx';wb.save(out);return FileResponse(out,filename='ROTA.xlsx')

@app.get('/users',response_class=HTMLResponse)
def users_page(request:Request,message:str=''):
    u=require(request,['owner','admin'])
    if not u:return RedirectResponse('/login',303)
    return templates.TemplateResponse(request=request,name='users.html',context={'u':u,'school':school(request),'users':rows('SELECT * FROM users WHERE school_id=? ORDER BY id',(u['school_id'],)),'message':message,'limit':PLANS[school(request)['plan']]['users']})
@app.post('/users')
def add_user(request:Request,name:str=Form(...),username:str=Form(...),email:str=Form(...),password:str=Form(...),role:str=Form('viewer')):
    u=require(request,['owner','admin']);
    if not u:return RedirectResponse('/login',303)
    username=username.strip()
    if not re.fullmatch(r'[A-Za-z0-9_.-]{3,30}',username):return RedirectResponse('/users?message='+quote('اسم المستخدم غير صالح'),303)
    n=one('SELECT COUNT(*) n FROM users WHERE school_id=?',(u['school_id'],))['n'];limit=PLANS[school(request)['plan']]['users']
    if n>=limit:return RedirectResponse('/users?message='+quote('وصلتِ للحد المسموح في باقتك'),303)
    try:exec1('INSERT INTO users(school_id,name,username,email,password_hash,role,verified,created_at) VALUES(?,?,?,?,?,?,?,?)',(u['school_id'],name.strip(),username,email.strip().lower(),pwhash(password),role,1,now()))
    except sqlite3.IntegrityError:return RedirectResponse('/users?message='+quote('اسم المستخدم أو الإيميل مستخدم مسبقًا'),303)
    return RedirectResponse('/users',303)


@app.post('/account/profile')
def account_profile(request:Request,name:str=Form(...),username:str=Form(...),email:str=Form(...)):
    u=require(request)
    if not u:return RedirectResponse('/login',303)
    username=username.strip();email=email.strip().lower()
    if not re.fullmatch(r'[A-Za-z0-9_.-]{3,30}',username):return RedirectResponse('/settings?message='+quote('اسم المستخدم غير صالح'),303)
    other=one('SELECT id FROM users WHERE lower(username)=lower(?) AND id<>?',(username,u['id']))
    if other:return RedirectResponse('/settings?message='+quote('اسم المستخدم مستخدم مسبقًا'),303)
    other=one('SELECT id FROM users WHERE lower(email)=lower(?) AND id<>?',(email,u['id']))
    if other:return RedirectResponse('/settings?message='+quote('الإيميل مستخدم مسبقًا'),303)
    email_changed=email.lower()!=u['email'].lower()
    exec1('UPDATE users SET name=?,username=?,email=?,verified=? WHERE id=?',(name.strip(),username,email,0 if email_changed else u['verified'],u['id']))
    if email_changed:
        dev=send_otp(u['id'],email);request.session['verify_uid']=u['id'];request.session['verify_mode']='signup'
        if dev:request.session['dev_otp']=dev
        return RedirectResponse('/verify',303)
    return RedirectResponse('/settings?message='+quote('تم تحديث الحساب'),303)

@app.post('/account/password')
def account_password(request:Request,current_password:str=Form(...),new_password:str=Form(...),confirm_password:str=Form(...)):
    u=require(request)
    if not u:return RedirectResponse('/login',303)
    if not pwcheck(current_password,u['password_hash']):return RedirectResponse('/settings?message='+quote('كلمة المرور الحالية غير صحيحة'),303)
    if len(new_password)<8:return RedirectResponse('/settings?message='+quote('كلمة المرور الجديدة يجب أن تكون 8 أحرف على الأقل'),303)
    if new_password!=confirm_password:return RedirectResponse('/settings?message='+quote('كلمتا المرور غير متطابقتين'),303)
    exec1('UPDATE users SET password_hash=? WHERE id=?',(pwhash(new_password),u['id']))
    return RedirectResponse('/settings?message='+quote('تم تغيير كلمة المرور'),303)

@app.get('/subscription',response_class=HTMLResponse)
def subscription(request:Request):
    u=require(request)
    if not u:return RedirectResponse('/login',303)
    return templates.TemplateResponse(request=request,name='subscription.html',context={'u':u,'school':school(request),'plans':PLANS,'pk':os.getenv('MOYASAR_PUBLISHABLE_KEY',''),'domain_ready':bool(os.getenv('PUBLIC_BASE_URL'))})
@app.post('/subscription/demo/{plan}')
def demo_plan(request:Request,plan:str):
    u=require(request,['owner']);
    if not u:return RedirectResponse('/login',303)
    if os.getenv('ALLOW_DEMO_PAYMENTS','0')=='1' and plan in PLANS:exec1("UPDATE schools SET plan=?,subscription_status='active',subscription_ends_at=? WHERE id=?",(plan,now()+30*86400,u['school_id']))
    return RedirectResponse('/subscription',303)
@app.get('/payment/callback')
def payment_callback(request:Request,id:str='',status:str=''):
    u=require(request)
    if not u:return RedirectResponse('/login',303)
    secret=os.getenv('MOYASAR_SECRET_KEY','')
    if not secret or not id:return RedirectResponse('/subscription',303)
    r=requests.get('https://api.moyasar.com/v1/payments/'+id,auth=(secret,''),timeout=20)
    if r.ok:
        p=r.json();meta=p.get('metadata') or {};plan=meta.get('plan','')
        if p.get('status')=='paid' and plan in PLANS and p.get('amount')==PLANS[plan]['price']*100 and p.get('currency')=='SAR':
            exec1("INSERT INTO payments(school_id,provider_payment_id,plan,amount,status,created_at) VALUES(?,?,?,?,?,?)",(u['school_id'],id,plan,p['amount'],'paid',now()));exec1("UPDATE schools SET plan=?,subscription_status='active',subscription_ends_at=? WHERE id=?",(plan,now()+30*86400,u['school_id']))
    return RedirectResponse('/subscription',303)

@app.get('/settings',response_class=HTMLResponse)
def settings_page(request:Request,message:str=''):
    u=require(request,['owner','admin'])
    if not u:return RedirectResponse('/login',303)
    return templates.TemplateResponse(request=request,name='settings.html',context={'u':u,'school':school(request),'message':message})
@app.post('/settings')
def settings_save(request:Request,school_name:str=Form(...),sender_phone:str=Form(''),sender_email:str=Form('')):
    u=require(request,['owner','admin']);
    if u:exec1('UPDATE schools SET name=?,sender_phone=?,sender_email=? WHERE id=?',(school_name.strip(),sender_phone.strip(),sender_email.strip(),u['school_id']))
    return RedirectResponse('/settings',303)

@app.get('/backup')
def backup(request:Request):
    u=require(request,['owner','admin'])
    if not u:return RedirectResponse('/login',303)
    p=backup_db('manual');return FileResponse(p,filename=p.name)
