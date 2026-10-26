'use strict';
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="dashboard-token"]').content;
let data = null, activeTab = 'keywords', timer = null, exiting = false, previousActive = false, activeDates = [];
let currentJob = null, revision = -1, polling = false, starting = false;
let loginPending = false, confirmationRevision = -1, cancelPending = false;
const labels = {idle:'대기',starting:'브라우저 실행 중',waiting_login:'로그인 대기',authenticating:'사용자 인증 진행',verifying:'로그인 확인 중',authenticated:'로그인 성공',collecting:'수집 중',completed:'완료',failed:'실패',cancelled:'취소',blocked:'접근 제한',cancelling:'취소 처리 중'};
const dayLabels = {waiting:'대기',collecting:'수집 중',success:'성공',failed:'실패',cancelled:'중단',skipped:'미실행'};
const node = (tag, text, cls) => {const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
function showError(message){$('error').textContent=message;$('error').hidden=!message;}
async function api(path, body){const response=await fetch(path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-Dashboard-Token':token},body:JSON.stringify(body)});const json=await response.json();if(!response.ok)throw new Error(json.error||'요청을 처리하지 못했습니다.');return json;}
function filterParams(){const p=new URLSearchParams();if($('filter-start').value)p.set('start',$('filter-start').value);if($('filter-end').value)p.set('end',$('filter-end').value);p.set('q',$('query').value);return p;}
async function loadData(){try{const next=await api('/api/data?'+filterParams());data=next;$('filter-start').value=data.start;$('filter-end').value=data.end;if(!$('collect-start').value){$('collect-start').value=data.start;$('collect-end').value=data.end;}render();showError('');}catch(e){showError(e.message);}}
function render(){
  const collected=data.daily.filter(d=>d.status!=='uncollected');$('metric-dates').textContent=collected.length+'일';$('metric-dates-note').textContent=`선택 ${data.daily.length}일 중 · 미수집 ${data.daily.length-collected.length}일`;
  $('metric-keywords').textContent=new Set(data.keywords.map(r=>r.raw_text)).size+'개';$('metric-repeated').textContent=data.repeated.length+'개';$('keyword-count').textContent=data.keywords.length;$('route-count').textContent=data.routes.length;
  $('daily').replaceChildren(...data.daily.map(d=>{const el=node('div',undefined,'day '+(d.status==='uncollected'?'uncollected':d.status==='empty'?'empty-day':''));el.append(node('strong',d.date),node('span',d.status==='uncollected'?'수집 안 함':d.status==='empty'?'수집 완료 · 검색어 없음':`수집 완료 · 검색어 ${d.keywords}개`));return el;}));
  renderTable();selectedDates();
}
function renderTable(){if(!data)return;const repeated=activeTab==='repeated';const rows=data[activeTab];const titles=repeated?['검색어 원문','등장한 날짜','등장 일수']:['기준일',activeTab==='keywords'?'검색어 원문':'상세 유입경로 원문','유입 비율'];const head=node('tr');head.append(...titles.map(t=>node('th',t)));$('table-head').replaceChildren(head);
  $('table-body').replaceChildren(...rows.map(r=>{const tr=node('tr');if(repeated){tr.append(node('td',r.raw_text),node('td',r.dates.join(', ')),node('td',r.days+'일'));}else{const ratio=node('td');const wrap=node('div',undefined,'ratio'),track=node('span',undefined,'ratio-track'),fill=node('span',undefined,'ratio-fill');fill.style.width=Math.max(0,Math.min(100,Number(r.ratio.replace('%',''))))+'%';track.append(fill);wrap.append(track,node('span',r.ratio.replace(/%$/,'')+'%'));ratio.append(wrap);tr.append(node('td',r.date),node('td',r.raw_text),ratio);}return tr;}));
  $('empty').hidden=rows.length>0;$('empty').textContent=repeated?'선택한 기간에 2일 이상 반복 등장한 검색어가 없습니다.':$('query').value?'필터에 맞는 저장 항목이 없습니다.':'표시할 항목이 없습니다. 날짜별 현황에서 미수집 여부를 확인하세요.';$('row-count').textContent=rows.length+'개 항목';
  $('table-note').textContent=repeated?'등장 일수는 검색어가 나타난 서로 다른 날짜 수입니다. 유입 횟수가 아닙니다.':'비율은 해당 날짜의 검색 유입 내 비율입니다. 실제 유입 횟수를 뜻하지 않습니다.';
  const p=filterParams();p.set('format','csv');p.set('category',activeTab==='routes'?'routes':'keywords');$('download-csv').href='/api/export?'+p;$('download-csv').textContent=repeated?'검색어 CSV ↓':'CSV ↓';p.set('format','xlsx');$('download-xlsx').href='/api/export?'+p;
}
function collectionDates(){const start=$('collect-start').value,end=$('collect-end').value;if(!start||!end||start>end)return[];if($('existing-only').checked){if(!data)return[];return data.daily.filter(d=>d.date>=start&&d.date<=end&&d.status!=='uncollected').map(d=>d.date);}const result=[];let d=new Date(start+'T00:00:00Z'),last=new Date(end+'T00:00:00Z');while(d<=last&&result.length<=366){result.push(d.toISOString().slice(0,10));d.setUTCDate(d.getUTCDate()+1);}return result;}
function selectedDates(){const dates=activeDates.length?activeDates:collectionDates();$('selected-dates').textContent=dates.length?`${dates.length}일 선택 · ${dates[0]} ~ ${dates.at(-1)}`:'수집할 날짜를 선택하세요.';}
async function poll(){
  if(exiting||polling)return;
  polling=true;
  try{
    const s=await api('/api/status'),j=s.job;
    if(exiting||j.revision<revision)return;
    revision=j.revision;currentJob=j.job_id;
    if(loginPending&&j.revision>confirmationRevision)loginPending=false;
    if(!j.active)cancelPending=false;
    activeDates=j.active?j.dates.map(d=>d.date):[];
    if(activeDates.length){
      $('collect-start').value=activeDates[0];$('collect-end').value=activeDates.at(-1);
      const span=(new Date(activeDates.at(-1)+'T00:00:00Z')-new Date(activeDates[0]+'T00:00:00Z'))/86400000+1;
      $('existing-only').checked=activeDates.length<span;
    }
    selectedDates();
    $('blog-id').textContent=s.blog_id;
    $('phase').textContent=labels[j.phase]||j.phase;
    $('phase').className='status-pill '+(['failed','blocked'].includes(j.phase)?'failed':j.phase==='completed'?'success':j.active?'running':'');
    $('job-message').textContent=j.message;
    $('progress').max=j.total||1;$('progress').value=j.completed;
    $('progress-count').textContent=`${j.completed} / ${j.total}일`;
    $('progress-percent').textContent=(j.total?Math.round(j.completed/j.total*100):0)+'%';
    $('login-complete').hidden=!['waiting_login','authenticating'].includes(j.phase)||!j.active;
    $('login-complete').disabled=loginPending||j.phase==='verifying';
    $('cancel').hidden=!j.active;$('cancel').disabled=cancelPending||j.phase==='cancelling';
    $('collect-button').disabled=j.active||s.stopping||starting;
    ['collect-start','collect-end','existing-only'].forEach(id=>$(id).disabled=j.active);
    $('job-dates').replaceChildren(...j.dates.map(d=>{const li=node('li');li.append(node('span',d.date),node('span',dayLabels[d.status]||d.status));return li;}));
    const yesterday=new Date(s.today+'T00:00:00Z');yesterday.setUTCDate(yesterday.getUTCDate()-1);
    ['collect-start','collect-end'].forEach(id=>$(id).max=yesterday.toISOString().slice(0,10));
    if(previousActive&&!j.active)await loadData();previousActive=j.active;
    $('connection').textContent=s.stopping?'● 종료 중':'● 내 컴퓨터에서 실행 중';
  }catch(e){
    showError('로컬 서버에 연결할 수 없습니다. 바탕화면 바로가기로 다시 실행하세요.');
    $('connection').textContent='● 연결 끊김';
  }finally{polling=false;if(!exiting)timer=setTimeout(poll,1000);}
}
$('filter-form').addEventListener('submit',e=>{e.preventDefault();loadData();});
document.querySelectorAll('[data-tab]').forEach(b=>b.addEventListener('click',()=>{activeTab=b.dataset.tab;document.querySelectorAll('[data-tab]').forEach(t=>{t.classList.toggle('active',t===b);t.setAttribute('aria-selected',String(t===b));});renderTable();}));
['collect-start','collect-end','existing-only'].forEach(id=>$(id).addEventListener('change',selectedDates));
$('collect-form').addEventListener('submit',async e=>{e.preventDefault();try{if($('existing-only').checked){const fresh=await api('/api/data?'+new URLSearchParams({start:$('collect-start').value,end:$('collect-end').value}));const dates=fresh.daily.filter(d=>d.status!=='uncollected').map(d=>d.date);if(!dates.length)throw new Error('선택한 기간에 저장된 날짜가 없습니다.');$('confirm-description').textContent=dates.length+'일을 다시 수집합니다.\n'+dates.join(', ');}else{const dates=collectionDates();if(!dates.length||dates.length>366)throw new Error('1일부터 366일 사이의 올바른 기간을 선택하세요.');$('confirm-description').textContent=dates[0]+' ~ '+dates.at(-1)+'\n총 '+dates.length+'일을 수집합니다.';}$('collect-dialog').showModal();}catch(err){showError(err.message);}});
$('confirm-cancel').addEventListener('click',()=>$('collect-dialog').close());
$('confirm-start').addEventListener('click',async()=>{if(starting||exiting)return;starting=true;$('collect-dialog').close();$('collect-button').disabled=true;try{await api('/api/collect',{start:$('collect-start').value,end:$('collect-end').value,existing_only:$('existing-only').checked,request_id:crypto.randomUUID()});showError('');}catch(e){showError(e.message);}finally{starting=false;}});
$('login-complete').addEventListener('click',async()=>{if(!currentJob||exiting||loginPending||$('login-complete').disabled)return;loginPending=true;confirmationRevision=revision;$('login-complete').disabled=true;try{await api('/api/login-complete',{job_id:currentJob,request_id:crypto.randomUUID()});}catch(e){loginPending=false;showError(e.message);} });
$('cancel').addEventListener('click',async()=>{if(!currentJob||exiting||cancelPending)return;cancelPending=true;$('cancel').disabled=true;try{await api('/api/cancel',{job_id:currentJob});}catch(e){cancelPending=false;showError(e.message);}});
$('shutdown').addEventListener('click',()=>$('shutdown-dialog').showModal());$('shutdown-cancel').addEventListener('click',()=>$('shutdown-dialog').close());
$('shutdown-confirm').addEventListener('click',async()=>{try{await api('/api/shutdown',{});exiting=true;clearTimeout(timer);$('shutdown-dialog').close();$('connection').textContent='● 종료 중';$('shutdown').disabled=true;$('collect-button').disabled=true;$('cancel').hidden=true;$('login-complete').hidden=true;$('job-message').textContent='현재 작업과 브라우저를 정리하고 있습니다. 이 페이지를 닫아도 됩니다.';const check=async()=>{try{await api('/health');setTimeout(check,700);}catch{$('connection').textContent='● 종료됨';$('phase').textContent='프로그램 종료';$('job-message').textContent='서버와 수집 브라우저를 종료했습니다. 저장된 데이터는 유지됩니다. 다시 보려면 바탕화면 바로가기를 실행하세요.';}};setTimeout(check,700);}catch(e){showError(e.message);}});
loadData();poll();
