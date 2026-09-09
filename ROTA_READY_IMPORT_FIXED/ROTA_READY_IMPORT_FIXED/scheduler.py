from ortools.sat.python import cp_model
DAYS_COUNT=5
PERIODS_COUNT=7

def enabled(cfg,key): return bool(cfg.get(key,{}).get('enabled'))
def val(cfg,key,default):
    try:return int(cfg.get(key,{}).get('value',default))
    except:return default

def solve_schedule(teachers,classes,assignments,unavailable,locked_lessons=None,cfg=None):
    cfg=cfg or {};locked_lessons=locked_lessons or []
    if not teachers or not classes or not assignments:return {'ok':False,'errors':['تأكدي من وجود المعلمات والفصول والإسنادات.']}
    model=cp_model.CpModel();x={}; penalties=[]
    for a in assignments:
        for d in range(DAYS_COUNT):
            for p in range(PERIODS_COUNT):x[(a['id'],d,p)]=model.NewBoolVar(f"x_{a['id']}_{d}_{p}")
    for a in assignments:model.Add(sum(x[(a['id'],d,p)] for d in range(DAYS_COUNT) for p in range(PERIODS_COUNT))==a['weekly_periods'])
    for t in teachers:
        aids=[a['id'] for a in assignments if a['teacher_id']==t['id']]
        for d in range(DAYS_COUNT):
            for p in range(PERIODS_COUNT):model.Add(sum(x[(a,d,p)] for a in aids)<=1)
            model.Add(sum(x[(a,d,p)] for a in aids for p in range(PERIODS_COUNT))<=t['max_daily'])
            maxc=2 if enabled(cfg,'avoid_three_consecutive') else max(1,int(t['max_consecutive']))
            for st in range(PERIODS_COUNT-maxc):model.Add(sum(x[(a,d,p)] for a in aids for p in range(st,st+maxc+1))<=maxc)
    for cl in classes:
        aids=[a['id'] for a in assignments if a['class_id']==cl['id']]
        for d in range(DAYS_COUNT):
            for p in range(PERIODS_COUNT):model.Add(sum(x[(a,d,p)] for a in aids)<=1)
    for z in unavailable:
        aids=[a['id'] for a in assignments if a['teacher_id']==z['teacher_id']];p=z['period']-1
        for a in aids:
            if z['level']=='hard':model.Add(x[(a,z['day'],p)]==0)
            else:penalties.append(x[(a,z['day'],p)]*z['weight'])
    for l in locked_lessons:model.Add(x[(l['assignment_id'],l['day'],l['period']-1)]==1)
    for a in assignments:
        name=str(a['subject_name'])
        if enabled(cfg,'math_no_period7') and 'رياض' in name:
            for d in range(DAYS_COUNT):model.Add(x[(a['id'],d,6)]==0)
        elif enabled(cfg,'other_period7_max'):
            # per assignment cap; weekly schedule only has 5 seventh-period slots anyway
            model.Add(sum(x[(a['id'],d,6)] for d in range(DAYS_COUNT))<=val(cfg,'other_period7_max',2))
        if enabled(cfg,'spread_lessons'):
            for d in range(DAYS_COUNT):
                daily=sum(x[(a['id'],d,p)] for p in range(PERIODS_COUNT));extra=model.NewIntVar(0,7,f"extra_{a['id']}_{d}");model.Add(extra>=daily-1);penalties.append(extra*6)
    # Double periods for art/digital: discourage isolated periods strongly; exact pairing is hard constraint for even weekly counts.
    if enabled(cfg,'art_digital_double'):
        for a in assignments:
            if any(k in str(a['subject_name']) for k in ['فنية','رقمية']) and a['weekly_periods']%2==0:
                starts=[]
                for d in range(DAYS_COUNT):
                    for p in range(PERIODS_COUNT-1):
                        b=model.NewBoolVar(f"pair_{a['id']}_{d}_{p}");model.Add(b<=x[(a['id'],d,p)]);model.Add(b<=x[(a['id'],d,p+1)]);model.Add(b>=x[(a['id'],d,p)]+x[(a['id'],d,p+1)]-1);starts.append(b)
                model.Add(sum(starts)>=a['weekly_periods']//2)
    if enabled(cfg,'reduce_gaps'):
        for t in teachers:
            aids=[a['id'] for a in assignments if a['teacher_id']==t['id']]
            for d in range(DAYS_COUNT):
                for p in range(1,6):
                    gap=model.NewBoolVar(f"gap_{t['id']}_{d}_{p}");before=sum(x[(a,d,p-1)] for a in aids);cur=sum(x[(a,d,p)] for a in aids);after=sum(x[(a,d,p+1)] for a in aids);model.Add(gap>=before+after-cur-1);penalties.append(gap*3)
    if enabled(cfg,'balance_edge_periods'):
        for t in teachers:
            aids=[a['id'] for a in assignments if a['teacher_id']==t['id']]
            for d in range(DAYS_COUNT):
                for a in aids:penalties.append(x[(a,d,6)]);penalties.append(x[(a,d,0)])
    if penalties:model.Minimize(sum(penalties))
    solver=cp_model.CpSolver();solver.parameters.max_time_in_seconds=45;solver.parameters.num_search_workers=8
    st=solver.Solve(model)
    if st not in (cp_model.OPTIMAL,cp_model.FEASIBLE):return {'ok':False,'errors':['تعذر إيجاد جدول يحقق القيود الحالية. جرّبي تخفيف أحد القيود الإلزامية.']}
    out=[]
    for a in assignments:
        for d in range(5):
            for p in range(7):
                if solver.Value(x[(a['id'],d,p)]):out.append({'assignment_id':a['id'],'day':d,'period':p+1})
    return {'ok':True,'lessons':out}
