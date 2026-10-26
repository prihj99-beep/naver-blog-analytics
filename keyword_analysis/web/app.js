'use strict';
const $ = selector => document.querySelector(selector);
const token = $('meta[name="analysis-token"]').content;
const colors = ['#14694c', '#366caa', '#ae6b28', '#8b5ba2', '#a84159'];
let topics = [], chosen = new Set(), editing = null;
let configured = false, demo = false, loading = true, busy = false, mutating = false;
let inboundBusy = false, closing = false, generation = 0;
let exportId = null, exporting = false;
let weeklyBusy = false, weeklyPreview = null, weeklyData = {reports: [], comparisons: []};
let analysisSequence = 0, previewTimer = null, weeklyVisibleCount = 5;

function node(tag, text, cls) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (cls) element.className = cls;
  return element;
}
function say(text, error = false) {
  $('#message').textContent = text;
  $('#message').className = error ? 'error' : '';
}
function trendStatus(text, error = false) {
  $('#trend-status').textContent = text;
  $('#trend-status').className = 'trend-status' + (error ? ' error' : '');
}
async function api(path, payload) {
  let response;
  try {
    response = await fetch(path, {
      method: payload === undefined ? 'GET' : 'POST', cache: 'no-store', redirect: 'error',
      headers: payload === undefined ? {} : {'Content-Type': 'application/json', 'X-Analysis-Token': token},
      body: payload === undefined ? undefined : JSON.stringify(payload)
    });
  } catch {
    throw new Error('로컬 서버에 연결할 수 없습니다. 실행 창을 확인하세요.');
  }
  let data;
  try { data = await response.json(); }
  catch { throw new Error('로컬 서버의 응답을 읽을 수 없습니다. 실행 창을 확인하세요.'); }
  if (!response.ok) {
    const message = response.status === 404 && path.startsWith('/api/weekly') ?
      '주간 기능 요청이 서버에서 404로 거절되었습니다. 이전 코드의 서버가 실행 중일 수 있습니다. 이 화면의 ‘분석 화면 종료’를 누른 뒤 start_keyword_analysis.bat를 다시 실행하세요. 탭 닫기나 새로고침만으로는 서버가 갱신되지 않습니다.' : data.error || '요청을 처리하지 못했습니다.';
    const error = new Error(message);
    error.code = data.code;
    throw error;
  }
  return data;
}
function showTab(name, focus = false) {
  for (const tab of document.querySelectorAll('[role="tab"]')) {
    const active = tab.id === 'tab-' + name;
    tab.setAttribute('aria-selected', String(active));
    tab.tabIndex = active ? 0 : -1;
    $('#' + tab.getAttribute('aria-controls')).hidden = !active;
    if (active && focus) tab.focus();
  }
}
const tabs = [...document.querySelectorAll('[role="tab"]')];
for (const [index, tab] of tabs.entries()) {
  tab.onclick = () => showTab(tab.id.slice(4));
  tab.onkeydown = event => {
    let next;
    if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
    if (event.key === 'ArrowLeft') next = (index + tabs.length - 1) % tabs.length;
    if (event.key === 'Home') next = 0;
    if (event.key === 'End') next = tabs.length - 1;
    if (next !== undefined && !closing) {
      event.preventDefault(); showTab(tabs[next].id.slice(4), true);
    }
  };
}
$('#go-manage').onclick = () => showTab('manage', true);
$('#go-trends').onclick = () => showTab('trends', true);

function requirements() {
  const reasons = [];
  if (loading) reasons.push('실행 상태를 확인하고 있습니다.');
  if (!topics.length) reasons.push('주제군을 먼저 등록하세요');
  else if (!chosen.size) reasons.push('분석할 주제군을 선택하세요');
  if (!configured && !demo) reasons.push('API 인증정보를 설정하세요');
  if (!$('#trend-end').value || !$('#trend-end').validity.valid) reasons.push('유효한 기준 종료일을 선택하세요');
  return reasons;
}
function syncControls() {
  const locked = loading || busy || mutating || closing || exporting || weeklyBusy;
  for (const element of document.querySelectorAll('main input, main textarea, main select, main button:not([role="tab"])')) {
    element.disabled = locked;
  }
  for (const tab of tabs) tab.disabled = closing;
  $('#query-inbound').disabled = locked || inboundBusy;
  const reasons = requirements();
  $('#query-requirements').replaceChildren(...reasons.map(text => node('li', text)));
  $('#query-trends').disabled = locked || reasons.length > 0;
  $('#query-trends').textContent = busy ? '응답 확인 중…' : '검색 트렌드 조회';
  $('#export-xlsx').disabled = locked || !$('#trend-end').value || !$('#trend-end').validity.valid;
  $('#weekly-export').disabled = $('#export-xlsx').disabled;
  $('#weekly-save').disabled = locked || !weeklyPreview || weeklyPreview.report.errors.length > 0;
}
function discard() {
  exportId = null;
  generation++;
  $('#chart').replaceChildren(); $('#chart').setAttribute('hidden', '');
  $('#chart-empty').hidden = false;
  $('#chart-empty').textContent = '조건을 확인하고 검색 트렌드를 조회하세요.';
  for (const id of ['legend', 'summaries', 'series-head', 'series-body']) $('#' + id).replaceChildren();
  $('#result-label').textContent = 'API 결과는 메모리에서만 표시합니다. 새 조회를 눌러주세요.';
  trendStatus('조회 대기 · 아직 새 API 요청을 보내지 않았습니다.');
  if (weeklyData.reports.length && !closing && document.visibilityState !== 'hidden') refreshWeeklyAnalysis();
}
function words() {
  return $('#topic-words').value.split(/\r?\n/).map(text => text.trim()).filter(Boolean);
}
function wordCount() { $('#word-count').textContent = `입력한 검색어 ${words().length}/5개`; }
$('#topic-words').oninput = wordCount;
function draftDirty() {
  const saved = topics.find(topic => topic.id === editing?.id);
  if (!saved) return Boolean($('#topic-name').value.trim() || words().length);
  return $('#topic-name').value.trim() !== saved.name || JSON.stringify(words()) !== JSON.stringify(saved.keywords);
}
function resetEditor() {
  editing = null; $('#topic-form').reset();
  $('#editor-title').textContent = '새 주제군';
  $('#edit-version').textContent = '저장할 때마다 버전이 기록됩니다.';
  wordCount();
}
function edit(topic, ask = true) {
  if (ask && draftDirty() && !confirm('저장하지 않은 작성 내용을 버리고 주제군을 다시 불러올까요?')) return false;
  editing = {id: topic.id, version: topic.version};
  $('#topic-name').value = topic.name;
  $('#topic-words').value = topic.keywords.join('\n');
  $('#editor-title').textContent = '주제군 수정';
  $('#edit-version').textContent = `현재 v${topic.version} · 저장 시 새 버전 생성`;
  wordCount(); showTab('manage'); $('#topic-name').focus();
  return true;
}
$('#reset-topic').onclick = () => {
  if (!draftDirty() || confirm('저장하지 않은 작성 내용을 비우고 새로 작성할까요?')) resetEditor();
};
function renderSelection() {
  const preview = $('#selected-topic-words'); preview.replaceChildren();
  for (const topic of topics.filter(topic => chosen.has(topic.id))) {
    const item = node('div', undefined, 'selected-topic');
    item.append(node('strong', `${topic.name} · v${topic.version}`), node('p', topic.keywords.join(' · ')));
    preview.append(item);
  }
  syncControls();
}
function renderTopics() {
  const host = $('#topics'), managed = $('#managed-topics');
  host.replaceChildren(); managed.replaceChildren();
  if (!topics.length) {
    host.append(node('p', '주제군을 먼저 등록하세요', 'hint'));
    managed.append(node('p', '아직 주제군이 없습니다. 오른쪽 작성란에서 등록하세요.', 'hint'));
  }
  for (const topic of topics) {
    const choice = node('div', undefined, 'topic'), label = node('label'), checkbox = node('input');
    checkbox.type = 'checkbox'; checkbox.checked = chosen.has(topic.id);
    checkbox.setAttribute('aria-label', `${topic.name} 선택`);
    checkbox.onchange = () => {
      if (checkbox.checked && chosen.size >= 5) {
        checkbox.checked = false; say('조회할 주제군은 최대 5개입니다.', true); return;
      }
      checkbox.checked ? chosen.add(topic.id) : chosen.delete(topic.id);
      discard(); renderSelection();
    };
    label.append(checkbox, node('span', `${topic.name} · v${topic.version}`));
    choice.append(label); host.append(choice);
    const card = node('div', undefined, 'topic');
    card.append(node('strong', `${topic.name} · v${topic.version}`), node('p', topic.keywords.join(' · ')));
    const actions = node('div', undefined, 'actions');
    const update = node('button', '수정', 'quiet small'), del = node('button', '삭제', 'quiet small');
    update.type = del.type = 'button';
    update.setAttribute('aria-label', `${topic.name} 수정`);
    del.setAttribute('aria-label', `${topic.name} 삭제`);
    update.onclick = () => edit(topic);
    del.onclick = async () => {
      if (busy || mutating || closing || !confirm(`‘${topic.name}’ 주제군을 삭제할까요? 기존 버전 이력은 보존됩니다.`)) return;
      mutating = true; syncControls();
      try {
        await api('/api/topics/delete', {id: topic.id, version: topic.version});
        if (closing) return;
        if (editing?.id === topic.id) resetEditor();
        discard(); await refresh(); say('주제군을 삭제했습니다.');
      } catch (error) { if (!closing) say(error.message, true); }
      finally { mutating = false; syncControls(); }
    };
    actions.append(update, del); card.append(actions); managed.append(card);
  }
  const target = $('#inbound-target'), previousTarget = target.value;
  target.replaceChildren(new Option('새 주제군 작성란', ''));
  for (const topic of topics) target.append(new Option(`${topic.name} · v${topic.version}`, topic.id));
  target.value = topics.some(topic => topic.id === previousTarget) ? previousTarget : '';
  renderSelection();
}
async function refresh() {
  const state = await api('/api/state');
  topics = state.topics; configured = state.configured; demo = state.demo;
  $('#auth-help').hidden = configured || demo;
  chosen = new Set([...chosen].filter(id => topics.some(topic => topic.id === id)));
  $('#mode').className = 'banner' + (demo ? ' demo' : '');
  $('#mode').textContent = demo ? '모의 응답 전용 · 실제 API 연결과 키 사용이 없습니다.' : configured ?
    'API 키 설정 확인됨 · 인증 성공 여부는 실제 조회 전까지 확인되지 않습니다.' :
    'API 키 미설정 · API 인증정보를 설정하세요. 주제군 관리와 기존 유입어 조회는 사용할 수 있습니다.';
  if (!$('#trend-end').value) {
    $('#trend-end').value = state.yesterday; $('#inbound-end').value = state.yesterday;
    const first = new Date(state.yesterday + 'T12:00:00Z'); first.setUTCDate(first.getUTCDate() - 13);
    $('#inbound-start').value = first.toISOString().slice(0, 10);
  }
  $('#trend-end').max = state.yesterday; $('#trend-end').min = '2016-01-14';
  loading = false; renderTopics(); periods(); syncControls();
  try { await refreshWeekly(); } catch { $('#weekly-status').textContent = '주간 자료를 읽지 못했습니다. 기존 검색 트렌드 기능은 사용할 수 있습니다.'; }
}
$('#topic-form').onsubmit = async event => {
  event.preventDefault(); if (busy || mutating || closing) return;
  const list = words();
  if (!list.length || list.length > 5 || new Set(list).size !== list.length || list.some(text => text.length > 200)) {
    say('중복 없이 1~5개의 검색어를 입력하세요. 검색어당 최대 200자입니다.', true); return;
  }
  mutating = true; syncControls();
  try {
    const topic = await api('/api/topics/save', {...editing, name: $('#topic-name').value, keywords: list});
    if (closing) return;
    if (chosen.size < 5) chosen.add(topic.id);
    resetEditor(); discard(); await refresh();
    say(`‘${topic.name}’ v${topic.version} 저장 완료. 아직 API를 호출하지 않았습니다.`);
  } catch (error) { if (!closing) say(error.message, true); }
  finally { mutating = false; syncControls(); }
};
function addInbound(rawText) {
  const targetId = $('#inbound-target').value;
  const target = topics.find(topic => topic.id === targetId);
  const sameDraft = targetId ? editing?.id === targetId : editing === null;
  const list = sameDraft ? words() : (target ? [...target.keywords] : []);
  if (list.includes(rawText)) { say('선택한 주제군의 작성란에 이미 있는 검색어입니다.'); return; }
  if (list.length >= 5) { say('주제군당 최대 5개입니다. 관리 탭에서 검색어를 정리하세요.', true); return; }
  if (!sameDraft && draftDirty() && !confirm('저장하지 않은 작성 내용을 버리고 선택한 주제군에 추가할까요?')) return;
  if (!sameDraft) { if (target) edit(target, false); else resetEditor(); }
  $('#topic-words').value = [...list, rawText].join('\n'); wordCount();
  showTab('manage'); $('#topic-words').focus();
  say('검색어 원문을 작성란에 추가했습니다. 주제군 저장을 눌러 확정하세요.');
}
$('#inbound-form').onsubmit = async event => {
  event.preventDefault(); if (busy || mutating || closing || inboundBusy) return;
  const params = new URLSearchParams({start: $('#inbound-start').value, end: $('#inbound-end').value, term: $('#inbound-term').value});
  inboundBusy = true; syncControls(); $('#inbound-body').replaceChildren();
  $('#inbound-status').textContent = '저장된 유입어를 읽고 있습니다.';
  try {
    const data = await api('/api/inbound?' + params);
    if (closing) return;
    for (const row of data.rows) {
      const tr = node('tr'), cell = node('td'), add = node('button', '추가', 'quiet small');
      add.type = 'button'; add.setAttribute('aria-label', `${row.raw_text} 추가`);
      add.onclick = () => addInbound(row.raw_text); cell.append(add);
      tr.append(node('td', row.date), node('td', row.raw_text), node('td', row.ratio + '%'), cell);
      $('#inbound-body').append(tr);
    }
    const counts = {uncollected: 0, empty: 0, collected: 0};
    for (const day of data.days) counts[day.status]++;
    $('#inbound-status').textContent = data.available ?
      `${data.rows.length}행 · 수집 ${counts.collected}일 / 검색어 없음 ${counts.empty}일 / 미수집 ${counts.uncollected}일. 유입 비율은 날짜별 원본 값입니다.` : data.message;
    say('저장된 유입어를 조회했습니다. API를 호출하지 않았습니다.');
  } catch (error) {
    if (!closing) { $('#inbound-status').textContent = '유입어 조회 실패'; say(error.message, true); }
  } finally { inboundBusy = false; syncControls(); }
};
function periods() {
  const end = $('#trend-end').value; if (!end) return;
  const date = new Date(end + 'T12:00:00Z'); if (Number.isNaN(date.getTime())) return;
  const at = offset => { const d = new Date(date); d.setUTCDate(d.getUTCDate() + offset); return d.toISOString().slice(0, 10); };
  $('#periods').textContent = `이전 7일 ${at(-13)} ~ ${at(-7)} / 최근 7일 ${at(-6)} ~ ${at(0)}`;
}
$('#trend-end').oninput = () => { periods(); discard(); syncControls(); };
function svg(tag, attrs = {}, text) {
  const element = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attrs)) element.setAttribute(key, String(value));
  if (text !== undefined) element.textContent = text;
  return element;
}
function number(value) { return value === null ? '—' : value.toLocaleString('ko-KR', {maximumFractionDigits: 2}); }
function render(data) {
  exportId = data.export_id;
  if (weeklyData.reports.length && !closing) refreshWeeklyAnalysis();
  $('#chart-empty').hidden = true;
  const chart = $('#chart'); chart.removeAttribute('hidden'); chart.replaceChildren();
  const x = i => 48 + i * 660 / 13, y = value => 230 - value * 2;
  for (const value of [0, 25, 50, 75, 100]) {
    chart.append(svg('line', {x1: 48, y1: y(value), x2: 708, y2: y(value), stroke: '#e0e8e2'}),
      svg('text', {x: 36, y: y(value) + 4, 'text-anchor': 'end'}, value));
  }
  chart.append(svg('line', {x1: x(6.5), x2: x(6.5), y1: 30, y2: 230, stroke: '#a0b5a8', 'stroke-dasharray': '5 5'}));
  for (const i of [0, 6, 7, 13]) chart.append(svg('text', {x: x(i), y: 255, 'text-anchor': 'middle'}, data.groups[0].points[i].date.slice(5)));
  const legend = $('#legend'), cards = $('#summaries'); legend.replaceChildren(); cards.replaceChildren();
  for (const [groupIndex, group] of data.groups.entries()) {
    const color = colors[groupIndex]; let path = '', connected = false;
    for (const [i, point] of group.points.entries()) {
      if (point.ratio === null) { connected = false; continue; }
      path += (connected ? 'L' : 'M') + `${x(i)},${y(point.ratio)} `; connected = true;
      chart.append(svg('circle', {cx: x(i), cy: y(point.ratio), r: 3, fill: color}));
    }
    chart.append(svg('path', {d: path, fill: 'none', stroke: color, 'stroke-width': 2.5}));
    const item = node('div', undefined, 'legend-item'), swatch = svg('svg', {viewBox: '0 0 18 8', 'aria-hidden': true});
    swatch.append(svg('line', {x1: 0, y1: 4, x2: 18, y2: 4, stroke: color, 'stroke-width': 3}));
    item.append(swatch, node('span', group.name)); legend.append(item);
    const card = node('article', undefined, 'summary-card');
    card.append(node('h3', `${group.name} · v${group.version}`),
      node('p', group.change_percent === null ? '변화율 표시 안 함' : `${group.change_percent > 0 ? '+' : ''}${number(group.change_percent)}%`, 'change'),
      node('p', `이전 평균 ${number(group.previous_mean)} → 최근 평균 ${number(group.recent_mean)}`),
      node('p', `지수 차이 ${number(group.delta)} · 유효 날짜 ${group.previous_count}/7 → ${group.recent_count}/7`, 'hint'));
    for (const warning of group.warnings) card.append(node('p', warning, 'warning'));
    cards.append(card);
  }
  const head = node('tr'); head.append(node('th', '날짜'));
  for (const group of data.groups) head.append(node('th', group.name));
  $('#series-head').replaceChildren(head); $('#series-body').replaceChildren();
  for (let i = 0; i < 14; i++) {
    const row = node('tr'); row.append(node('td', data.groups[0].points[i].date));
    for (const group of data.groups) row.append(node('td', group.points[i].ratio === null ? '누락' : number(group.points[i].ratio)));
    $('#series-body').append(row);
  }
  $('#result-label').textContent = (data.demo ? '[모의 데이터] ' : '') + `${data.start} ~ ${data.end} · ${data.notice}`;
}
$('#trend-form').onsubmit = async event => {
  event.preventDefault(); if (busy || mutating || closing || requirements().length) return;
  discard(); const revision = generation;
  const selection = topics.filter(topic => chosen.has(topic.id)).map(topic => ({id: topic.id, version: topic.version}));
  busy = true; syncControls(); trendStatus('조회 중 · 선택한 14일을 한 번 요청합니다. 자동 재시도하지 않습니다.');
  try {
    const data = await api('/api/trends', {selection, end: $('#trend-end').value});
    if (revision !== generation) return;
    render(data);
    trendStatus(data.groups.some(group => group.warnings.length) ? '조회 완료 · 일부 비교값에 제한이 있습니다. 안내를 확인하세요.' : '14일 조회와 두 구간 비교를 완료했습니다.');
  } catch (error) {
    if (revision !== generation) return;
    if (error.code === 'missing_key') { configured = false; $('#auth-help').hidden = demo; }
    trendStatus(error.message, true); say('검색 트렌드 조회 실패 · 원인은 검색 트렌드 탭에서 확인하세요.', true);
    $('#chart-empty').textContent = '조회에 실패했습니다. 이전 응답은 표시하지 않습니다.';
  } finally { if (revision === generation && !closing) { busy = false; syncControls(); } }
};
$('#shutdown').onclick = async () => {
  if (closing || !confirm('독립 분석 화면을 종료할까요? 기존 통계 수집기는 종료하지 않습니다.')) return;
  closing = true; $('#shutdown').disabled = true; discard(); syncControls();
  try { await api('/api/shutdown', {}); say('분석 서버가 종료되었습니다. 이 탭과 실행 창을 닫아도 됩니다.'); }
  catch (error) { closing = false; busy = false; $('#shutdown').disabled = false; syncControls(); say(error.message, true); }
};
async function downloadExport() {
  if (exporting || busy || mutating || closing || loading) return;
  exporting = true; syncControls();
  const revision = generation, end = $('#trend-end').value;
  try {
    const response = await fetch('/api/export', {method: 'POST', cache: 'no-store', redirect: 'error',
      headers: {'Content-Type': 'application/json', 'X-Analysis-Token': token},
      body: JSON.stringify({end, export_id: exportId})});
    if (!response.ok) { const data = await response.json(); throw new Error(data.error || '내보내기에 실패했습니다.'); }
    const blob = await response.blob();
    if (closing || revision !== generation) return;
    const missing = Number(response.headers.get('X-Uncollected-Days'));
    if (missing && !confirm(`선택한 14일 중 ${missing}일의 블로그 통계가 미수집입니다. 미수집으로 표시하여 다운로드할까요?`)) {
      say('다운로드를 취소했습니다. 현재 조회 결과는 유지됩니다.'); return;
    }
    const url = URL.createObjectURL(blob), link = node('a');
    link.href = url; link.download = `chatgpt-blog-analysis_${end}.xlsx`;
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
    say('엑셀 다운로드를 시작했습니다. 추가 네이버 API 호출은 없습니다.');
  } catch (error) { if (!closing) say(error.message || '엑셀 다운로드에 실패했습니다.', true); }
  finally { exporting = false; syncControls(); }
}
$('#export-xlsx').onclick = downloadExport;
$('#weekly-export').onclick = downloadExport;

function weeklyRow(host, values) {
  const row = node('tr');
  for (const value of values) row.append(node('td', value === null || value === undefined ? '미확인' : String(value)));
  host.append(row);
}
function weeklyKeywordRow(host, record) {
  const row = node('tr');
  row.append(node('td', record.raw_text), node('td', record.ratio));
  const cell = node('td', '미확인 · 입력에서 상세 링크를 확인하지 못함');
  if (record.detail_url) {
    // Only server-validated HTTPS detail URLs become clickable.
    const url = new URL(record.detail_url);
    if (url.protocol === 'https:' && url.host === 'creator-advisor.naver.com' && url.pathname === '/new-windows/query-stats' && !url.hash && !url.username && !url.password) {
      const link = node('a', record.detail_url);
      link.href = record.detail_url; link.target = '_blank'; link.rel = 'noopener noreferrer';
      cell.replaceChildren(link);
    }
  }
  row.append(cell); host.append(row);
}

function weeklyClipboardText(plain, html) {
  if (!html || /\[[^\]\n]+\]\(https?:\/\//.test(plain)) return {text: plain, count: 0};
  if (html.length > 2000000) throw new Error('HTML이 너무 큽니다. 검색어 목록만 다시 복사하세요.');
  // Template contents stay inert: never attach copied HTML to the live document.
  const template = document.createElement('template'); template.innerHTML = html;
  const groups = new Map();
  for (const anchor of template.content.querySelectorAll('a[href]')) {
    const href = anchor.getAttribute('href');
    if (!href.includes('/new-windows/query-stats')) continue;
    if (/[\s()]/.test(href)) throw new Error('상세 URL 형식을 확인할 수 없습니다.');
    let url;
    try { url = new URL(href); } catch { throw new Error('상세 URL 형식을 확인할 수 없습니다.'); }
    const words = url.searchParams.getAll('query');
    if (words.length !== 1 || !words[0] || /[\[\]\r\n]/.test(words[0])) throw new Error('상세 URL의 검색어를 확인할 수 없습니다.');
    const word = words[0], label = anchor.textContent.trim();
    const old = groups.get(word);
    if (old && old.href !== href) throw new Error('같은 검색어에 서로 다른 상세 URL이 있습니다.');
    const group = old || {href, word, keyword: false, ratios: []};
    const combined = label.match(/^(.*?)\s*(\d+(?:\.\d+)?)\s*%$/);
    if (label === word) group.keyword = true;
    else if (combined && (!combined[1].trim() || combined[1].trim() === word)) {
      if (combined[1].trim() === word) group.keyword = true;
      group.ratios.push(combined[2]);
    } else throw new Error('HTML 링크의 검색어·비율이 URL과 일치하지 않습니다.');
    groups.set(word, group);
  }
  if (!groups.size) return {text: plain, count: 0};
  const lines = plain.split(/\r?\n/), begin = lines.findIndex(line => line.trim() === '유입검색어 분석');
  if (begin < 0) throw new Error('일반 텍스트의 유입검색어 분석 영역을 확인할 수 없습니다.');
  const used = new Set(); let started = false;
  for (let i = begin+1; i < lines.length; i++) {
    const line = lines[i].trim();
    if (started && ['게시글','동영상','클립'].includes(line)) break;
    const same = line.match(/^(.*?)\s*(\d+(?:\.\d+)?)\s*%$/);
    const next = (lines[i+1] || '').trim().match(/^(\d+(?:\.\d+)?)\s*%$/);
    const word = same ? same[1].trim() : line;
    const ratio = same ? same[2] : next?.[1];
    const group = groups.get(word);
    if (!group || ratio === undefined) continue;
    if (!group.keyword || !group.ratios.length || group.ratios.some(value => Number(value) !== Number(ratio))) {
      throw new Error('HTML과 일반 텍스트의 검색어·비율을 명확히 대응할 수 없습니다.');
    }
    lines[i] = `[${word} ${ratio}%](${group.href})`;
    if (!same) lines[++i] = '';
    used.add(word); started = true;
  }
  if (used.size !== groups.size) throw new Error('일반 텍스트와 대응하지 않는 HTML 상세 링크가 있습니다.');
  return {text: lines.join('\n'), count: used.size};
}

$('#weekly-text').addEventListener('paste', event => {
  const clipboard = event.clipboardData;
  if (!clipboard) return;
  const plain = clipboard.getData('text/plain'), html = clipboard.getData('text/html');
  const status = $('#weekly-clipboard-status');
  try {
    const result = weeklyClipboardText(plain, html);
    if (result.count) {
      event.preventDefault();
      const input = event.currentTarget;
      input.setRangeText(result.text, input.selectionStart, input.selectionEnd, 'end');
      input.dispatchEvent(new Event('input', {bubbles: true}));
    }
    // No clipboard contents are logged or transmitted to any external service.
    status.textContent = `클립보드: 일반 텍스트 ${plain ? '있음' : '없음'} · HTML ${html ? '있음' : '없음'} · ` +
      (result.count ? `상세 URL ${result.count}개를 Markdown으로 변환했습니다. 미리보기를 확인하세요.` : '일반 텍스트를 유지합니다. URL 추출 여부는 미리보기에서 확인하세요.');
  } catch (error) {
    event.preventDefault();
    status.textContent = `붙여넣기를 적용하지 않았습니다. ${error.message} HTML 구조를 확인하거나 일반 텍스트로 붙여넣으세요.`;
  }
});
function renderWeeklyComparison() {
  const body = $('#weekly-compare-body'); body.replaceChildren();
  for (const row of weeklyData.comparisons.filter(row => row.start === $('#weekly-period').value)) {
    const status = row.status==='사라짐(목록 기준)' ? '이번 목록에서 미확인' : row.status==='신규(목록 기준)' ? '신규 관측(목록 기준)' : row.status;
    weeklyRow(body, [row.raw_text, status, row.previous_ratio, row.ratio, row.difference_pp]);
  }
  refreshWeeklyAnalysis();
}
function weeklyHistoryOrder() {
  return [...weeklyData.reports].sort((a,b)=>b.start.localeCompare(a.start));
}
function mostRecentlySaved(reports) {
  return [...reports].sort((a,b)=>{
    const time = value => Number.isFinite(Date.parse(value)) ? Date.parse(value) : 0;
    return time(b.saved_at)-time(a.saved_at) || b.start.localeCompare(a.start);
  })[0]?.start || '';
}
function renderWeeklyHistory() {
  const reports = weeklyHistoryOrder(), body = $('#weekly-saved-body'); body.replaceChildren();
  for (const report of reports.slice(0,weeklyVisibleCount)) {
    weeklyRow(body,[report.start,report.end,report.channel_views,report.search_rate,report.rows.length,report.revision]);
  }
  const count = Math.min(weeklyVisibleCount,reports.length);
  $('#weekly-history-status').textContent = reports.length ? `전체 ${reports.length}주 중 최근 ${count}주 표시` : '저장된 주간 자료가 없습니다.';
  $('#weekly-history-more').hidden = count >= reports.length;
}
function updateWeeklyMonths() {
  const select = $('#weekly-month'), old = select.value, year = $('#weekly-year').value;
  const months = [...new Set(weeklyData.reports.filter(r=>!year || r.start.slice(0,4)===year).map(r=>r.start.slice(5,7)))].sort().reverse();
  select.replaceChildren(new Option('전체 월',''));
  for (const month of months) select.append(new Option(`${Number(month)}월`,month));
  if (months.includes(old)) select.value = old;
}
function updateWeeklyPeriods(preferredStart) {
  const select = $('#weekly-period'), old = preferredStart || select.value;
  const year = $('#weekly-year').value, month = $('#weekly-month').value;
  const reports = weeklyHistoryOrder().filter(r=>(!year || r.start.slice(0,4)===year) && (!month || r.start.slice(5,7)===month));
  select.replaceChildren();
  for (const report of reports) select.append(new Option(`${report.start} ~ ${report.end}`,report.start));
  if (!reports.length) select.append(new Option('선택할 주간 자료 없음',''));
  select.value = reports.some(r=>r.start===old) ? old : mostRecentlySaved(reports);
  renderWeeklyComparison();
}
async function refreshWeekly(preferredStart) {
  weeklyData = await api('/api/weekly');
  const select = $('#weekly-period'), previous = preferredStart || select.value;
  weeklyVisibleCount = 5; renderWeeklyHistory();
  const year = $('#weekly-year'), oldYear = year.value;
  const years = [...new Set(weeklyData.reports.map(r=>r.start.slice(0,4)))].sort().reverse();
  year.replaceChildren(new Option('전체 연도',''));
  for (const value of years) year.append(new Option(`${value}년`,value));
  if (years.includes(oldYear)) year.value = oldYear;
  // A newly saved report must stay reachable even when an older range was selected.
  if (preferredStart) { year.value=''; $('#weekly-month').value=''; }
  updateWeeklyMonths(); updateWeeklyPeriods(previous);
}
$('#weekly-history-more').onclick = () => {
  if (closing) return;
  weeklyVisibleCount += 5; renderWeeklyHistory();
};
$('#weekly-year').onchange = () => { if (!closing) { updateWeeklyMonths(); updateWeeklyPeriods(); } };
$('#weekly-month').onchange = () => { if (!closing) updateWeeklyPeriods(); };
$('#weekly-period').onchange = renderWeeklyComparison;
function clearWeeklyPreview() {
  clearTimeout(previewTimer); weeklyPreview = null;
  for (const id of ['weekly-preview-meta', 'weekly-preview-body', 'weekly-errors', 'weekly-warnings', 'weekly-diff']) $('#'+id).replaceChildren();
  $('#weekly-status').textContent = '입력 내용이 변경되었습니다. 다시 미리보기 하세요.';
  syncControls();
}
function scheduleWeeklyPreview() {
  clearWeeklyPreview();
  if (!$('#weekly-text').value.trim()) return;
  $('#weekly-status').textContent = '붙여넣은 내용을 잠시 후 자동으로 미리보기합니다.';
  previewTimer = setTimeout(() => {
    if (!weeklyBusy && !busy && !mutating && !exporting && !closing) $('#weekly-form').requestSubmit();
  }, 650);
}
$('#weekly-text').oninput = () => { $('#weekly-coverage').value = 'unknown'; scheduleWeeklyPreview(); };
$('#weekly-coverage').onchange = scheduleWeeklyPreview;
$('#weekly-form').onsubmit = async event => {
  event.preventDefault();
  if (weeklyBusy || busy || exporting || mutating || closing) return;
  clearWeeklyPreview(); weeklyBusy = true; syncControls();
  $('#weekly-status').textContent = '로컬에서 추출 결과를 확인하고 있습니다.';
  const text = $('#weekly-text').value, coverage = $('#weekly-coverage').value;
  try {
    const preview = await api('/api/weekly/preview', {text, coverage});
    if (closing) return;
    weeklyPreview = {...preview, text, coverage};
    const report = preview.report;
    weeklyRow($('#weekly-preview-meta'), [report.start, report.end, report.channel_views, report.search_rate, report.channel]);
    for (const row of report.rows) weeklyKeywordRow($('#weekly-preview-body'), row);
    for (const message of report.errors) $('#weekly-errors').append(node('p', message));
    for (const message of report.warnings) $('#weekly-warnings').append(node('p', message));
    $('#weekly-status').textContent = `${report.rows.length}개 검색어 추출 · ${report.errors.length ? '오류를 확인하세요. 저장할 수 없습니다.' : '내용을 확인하고 저장하세요.'}`;
    const diff = preview.diff, host = $('#weekly-diff');
    host.append(node('p', preview.identical ? '같은 원문이 이미 저장되어 있습니다. 다시 저장해도 중복되지 않습니다.' : preview.existing_revision ? `동일 기간 v${preview.existing_revision} 자료가 있습니다. 아래 변경을 확인하면 교체할 수 있습니다.` : '새 주간 자료입니다.'));
    if (preview.existing_revision) {
      host.append(node('p', `추가 ${diff.added.length}개: ${diff.added.join(' · ')} / 삭제 ${diff.removed.length}개: ${diff.removed.join(' · ')}`));
      for (const row of diff.changed) host.append(node('p', `${row.raw_text}: ${row.before.ratio}% → ${row.after.ratio}% / URL ${row.before.detail_url === row.after.detail_url ? '동일' : '변경'}`));
      const names = {channel_views: '채널 조회수', search_rate: '검색 유입률', channel: '채널', coverage: '목록 범위(full=전체 사용자 확인, partial=일부, unknown=미확인)'};
      for (const row of diff.metrics) host.append(node('p', `${names[row.field]}: ${row.before ?? '미확인'} → ${row.after ?? '미확인'}`));
    }
  } catch (error) { if (!closing) $('#weekly-status').textContent = error.message; }
  finally { weeklyBusy = false; syncControls(); }
};
$('#weekly-save').onclick = async () => {
  if (weeklyBusy || busy || exporting || mutating || closing || !weeklyPreview || weeklyPreview.report.errors.length) return;
  const preview = weeklyPreview;
  if (preview.existing_revision && !preview.identical && !confirm('동일 기간 자료를 표시된 내용으로 교체할까요? 이전 원문은 보존됩니다.')) return;
  weeklyBusy = true; syncControls();
  try {
    const result = await api('/api/weekly/save', {text: preview.text, expected_revision: preview.existing_revision, acknowledge: true, coverage: preview.coverage});
    if (closing) return;
    clearWeeklyPreview(); await refreshWeekly(result.report.start);
    $('#weekly-status').textContent = result.saved ? `${result.report.start} ~ ${result.report.end} v${result.report.revision} 저장 완료` : '이미 같은 자료가 있습니다. 중복 저장하지 않았습니다.';
    say('주간 자료를 별도 DB에 저장했습니다. 네이버 요청과 기존 DB 변경은 없습니다.');
    $('#weekly-analysis').scrollIntoView({block:'start'}); $('#weekly-analysis').focus({preventScroll:true});
  } catch (error) { if (!closing) { clearWeeklyPreview(); $('#weekly-status').textContent = error.message + ' 다시 미리보기 하세요.'; } }
  finally { weeklyBusy = false; syncControls(); }
};

async function refreshWeeklyAnalysis() {
  const sequence = ++analysisSequence, start = $('#weekly-period').value;
  const content = $('#weekly-analysis-content'), status = $('#weekly-analysis-status');
  if (closing) return;
  content.hidden = true;
  if (!start) { status.textContent = '저장된 주간 자료가 없습니다. 붙여넣고 확인 저장하면 분석이 표시됩니다.'; return; }
  status.textContent = '저장된 자료로 분석 중입니다. 외부 API는 호출하지 않습니다.';
  try {
    const data = await api('/api/weekly/analysis', {start, export_id: exportId});
    if (closing || sequence !== analysisSequence) return;
    if (!data.available) { status.textContent = data.message; return; }
    if (!data.briefing || !Object.hasOwn(data.briefing.highlights?.[0] || {}, 'value')) throw new Error('새 요약을 표시하려면 분석 화면 종료 후 start_keyword_analysis.bat를 다시 실행해 주세요.');
    renderWeeklyBriefing(data.briefing, data);
    for (const id of ['quality','changes','topics','candidates','trend']) $('#weekly-analysis-'+id).replaceChildren();
    const quality = $('#weekly-analysis-quality');
    quality.append(node('p', `${data.start} ~ ${data.end} · v${data.revision} · ${data.coverage_label}`),
      node('p', `직전 주 ${data.previous_start} ~ ${data.previous_end} · ${data.previous_coverage_label}`),
      node('p', '주제 분류는 현재 저장된 주제군과 고정 규칙을 사용합니다. ' + data.rule_version, 'hint'));
    const warningKeys = new Set();
    for (const warning of [...new Set([...data.caveats,...data.warnings])]) {
      const key = warning.includes('더보기') ? 'more' : warning.includes('목록의 전체성') || warning.includes('범위 미확인 목록') ? 'coverage' : warning;
      if (!warningKeys.has(key)) quality.append(node('p', warning, 'warning'));
      warningKeys.add(key);
    }
    const changes = $('#weekly-analysis-changes');
    const table = node('table'), wrap = node('div', undefined, 'table-wrap');
    const head = node('tr'); for (const text of ['지표','이전 주','이번 주','차이']) head.append(node('th',text)); table.append(head);
    const body = node('tbody'); table.append(body); wrap.append(table); changes.append(wrap);
    for (const [field,label] of [['channel_views','채널 조회수(회)'],['search_rate','검색 유입률(%)']]) {
      const m = data.metrics[field]; weeklyRow(body,[label,m.previous,m.current,m.difference===null ? null : `${m.difference>0?'+':''}${m.difference}${m.unit}`]);
    }
    for (const text of data.explanations) changes.append(node('p',text));
    for (const text of data.briefing.changes) changes.append(node('p',text));
    const counts = new Map();
    for (const row of data.comparisons) counts.set(row.status, (counts.get(row.status)||0)+1);
    changes.append(node('p',[...counts].map(([key,value])=>`${key} ${value}개`).join(' · ')));
    const cmpTable = node('table'), cmpHead = node('tr'), cmpBody = node('tbody'), cmpWrap = node('div',undefined,'table-wrap');
    for (const text of ['검색어 원문','목록 비교','이전 비율(%)','현재 비율(%)','차이(%p)']) cmpHead.append(node('th',text));
    cmpTable.append(cmpHead,cmpBody); cmpWrap.append(cmpTable); changes.append(cmpWrap);
    for (const row of data.comparisons) weeklyRow(cmpBody,[row.raw_text,row.status,row.previous_ratio,row.ratio,row.difference_pp]);
    const topicHost = $('#weekly-analysis-topics');
    for (const group of data.topic_groups) {
      const section = node('section'); section.append(node('h5',`${group.name} · 검색어 ${group.rows.length}개`));
      for (const row of group.rows) {
        const card = node('article',undefined,'summary-card');
        card.append(node('strong',`${row.raw_text} · 실제 유입 비율 ${row.ratio}%`),node('p',row.labels.join(' / ')),node('p',row.reasons.join(' / '),'hint'));
        section.append(card);
      }
      topicHost.append(section);
    }
    const candidates = $('#weekly-analysis-candidates');
    const candidateChecks = new Set();
    if (!data.candidates.length) candidates.append(node('p','분류 근거가 충분한 후보가 없습니다. 미분류 검색어의 질문 의도를 직접 확인하세요.'));
    for (const item of data.candidates) {
      const card = node('article',undefined,'summary-card');
      card.append(node('strong',item.topic),node('p',item.candidate),node('p','실제 검색어: '+item.evidence.join(' · ')));
      candidateChecks.add(item.checks);
      candidates.append(card);
    }
    for (const check of candidateChecks) candidates.append(node('p',check,'warning'));
    renderWeeklyTrend(data.trend);
    content.hidden = false; status.textContent = `${data.start} ~ ${data.end} 저장 자료 분석 완료`;
  } catch (error) {
    if (!closing && sequence === analysisSequence) status.textContent = '저장 자료는 유지됩니다. 분석 표시 실패: '+error.message;
  }
}
// Shared presentation helpers: source values and full explanations are retained.
function briefingCard(title, className='brief-theme', tag='article') {
  const card = node(tag,undefined,className); card.append(node('h5',title)); return card;
}
function briefingDetail(label, children) {
  const details = node('details',undefined,'brief-detail');
  details.append(node('summary',label),...children); return details;
}
function keywordChips(words) {
  const list = node('div',undefined,'keyword-chips');
  for (const word of words) list.append(node('span',word,'keyword-chip'));
  return list;
}
function renderWeeklyBriefing(briefing, data) {
  for (const id of ['metrics','summary','notices','themes','changes','actions']) $('#weekly-brief-'+id).replaceChildren();
  for (const [field,label,unit] of [['channel_views','채널 조회수','회'],['search_rate','검색 유입 비율','%']]) {
    const m = data.metrics[field], card = briefingCard(label,'brief-metric');
    const value = node('p',m.current === null ? '자료 없음' : `${m.current}${unit}`,'brief-value');
    value.id = 'weekly-brief-value-'+field;
    const delta = m.difference === null ? '' : `${m.difference>0?'+':''}${m.difference}${m.unit}`;
    const change = node('p',delta,'brief-delta'); change.id = 'weekly-brief-delta-'+field; change.hidden = m.difference === null;
    const comparison = m.previous === null ? '직전 주 비교 자료 없음' : `지난주 ${m.previous}${unit} → 이번 주 ${m.current === null ? '자료 없음' : m.current+unit}`;
    card.append(value,change,node('p',comparison,'hint'));
    if (field==='search_rate') card.append(node('p','비율의 차이(%p)이며 유입 횟수의 변화가 아니에요.','hint'));
    $('#weekly-brief-metrics').append(card);
  }
  const themes = briefing.themes.filter(theme=>!theme.other);
  const priorities = briefing.actions.map(action=>action.topic);
  const featured = [...themes].sort((a,b)=>{
    const rank = name => priorities.includes(name) ? priorities.indexOf(name) : priorities.length;
    return rank(a.name)-rank(b.name);
  }).slice(0,3);
  for (const theme of featured) {
    const card = briefingCard(theme.name,'brief-feature-card');
    const line = theme.status.includes('지난주에도') ? '두 주에 이어서 관련 검색어 확인' : theme.status.includes('지난주 목록에는') ? '이번 주 복사 목록에서 확인한 관심사' : '이번 주 검색어에서 확인한 관심사';
    card.append(node('p',line)); $('#weekly-brief-summary').append(card);
  }
  if (!featured.length) $('#weekly-brief-summary').append(node('p','주요 관심사로 요약할 검색어가 충분하지 않아요.'));
  for (const note of new Set(briefing.notices)) $('#weekly-brief-notices').append(node('p',note));
  briefing.themes.forEach((theme,index) => {
    const card = briefingCard(theme.name);
    card.prepend(node('p',theme.other ? '분류 보류 · 공통 주제가 아닙니다' : '자동 분류 제목','brief-label'));
    card.append(node('span',`관련 검색어 ${theme.words.length}개`,'brief-count'),node('p',theme.description),node('p','실제 유입 검색어','brief-label'));
    const rows = data.topic_groups[index].rows;
    const makeTable = records => {
      const body = node('tbody'), table = node('table'), head = node('tr'), wrap = node('div',undefined,'table-wrap');
      for (const text of ['검색어 원문','개별 비율']) head.append(node('th',text));
      table.append(head,body); wrap.append(table);
      for (const row of records) weeklyRow(body,[row.raw_text,row.ratio === null ? '미확인' : row.ratio+'%']);
      return wrap;
    };
    card.append(makeTable(rows.slice(0,2)));
    if (rows.length>2) card.append(briefingDetail(`나머지 검색어 ${rows.length-2}개 보기`,[makeTable(rows.slice(2))]));
    $('#weekly-brief-themes').append(card);
  });
  const list = node('div',undefined,'brief-grid brief-grid-three');
  for (const change of briefing.highlights || []) {
    if (!change.value) continue;
    const card = briefingCard(change.title,'brief-theme brief-change');
    card.append(node('p',change.value,'brief-change-value'),node('p',change.caption,'hint'),briefingDetail('비교 근거 보기',[node('p',change.description)])); list.append(card);
  }
  if (!list.childElementCount) list.append(node('p','직전 주와 비교할 자료가 충분하지 않아요. 자세한 분석에서 자료 상태를 확인해 주세요.'));
  $('#weekly-brief-changes').append(list);
  for (const action of briefing.actions.slice(0,3)) {
    const item = node('li',undefined,'brief-action');
    item.append(node('p','추천 글 제목','brief-label'),node('h5',action.title),node('p','추천 이유','brief-label'),node('p',action.short_reason),keywordChips(action.evidence.slice(0,2)),node('p','확인할 점','brief-label'),node('p','기존 글·원자료 확인 후 발행 검토','hint'),briefingDetail('추천 근거와 확인할 점',[node('p',action.why_first),keywordChips(action.evidence),node('p',action.checks)]));
    $('#weekly-brief-actions').append(item);
  }
  if (!briefing.actions.length) $('#weekly-brief-actions').append(node('li','지금 자료만으로 추천할 글 주제가 충분하지 않아요.'));
  $('#weekly-brief-action-note').textContent = briefing.action_note;
}
function renderWeeklyTrend(trend) {
  const host = $('#weekly-analysis-trend'); host.replaceChildren();
  host.append(node('p',trend.state==='expired' ? '현재 트렌드 조회 결과가 만료되었습니다. 새 API 호출 없이 기본 분석만 표시합니다.' : trend.notice));
  if (trend.state !== 'available') return;
  host.append(node('p',`${trend.demo?'[모의 데이터] ':''}${trend.start} ~ ${trend.end} · 조회 시각 ${trend.fetched_at ?? '미확인'}`,'hint'));
  for (const group of trend.groups) {
    const card = node('article',undefined,'summary-card');
    card.append(node('strong',`${group.name} · 조회 당시 v${group.version}`),node('p','조회 구성 검색어: '+group.keywords.join(' · ')),
      node('p',`데이터랩 상대지수 평균: 이전 ${number(group.previous_mean)} → 최근 ${number(group.recent_mean)}`),node('p',group.notice,'hint'));
    if (!group.matching.length) card.append(node('p','이 목록에서 연결 근거가 있는 실제 유입어가 없습니다.'));
    for (const match of group.matching) card.append(node('p',`${match.raw_text} · 블로그 실제 유입 비율 ${match.ratio}% · ${match.basis}`));
    for (const warning of group.warnings) card.append(node('p',warning,'warning'));
    host.append(card);
  }
}
window.addEventListener('pagehide', discard);
refresh().then(() => say('주제군을 선택해 검색 트렌드를 확인하거나, 다른 탭에서 유입어와 주제군을 관리하세요.'))
  .catch(error => { loading = false; syncControls(); say(error.message, true); });
