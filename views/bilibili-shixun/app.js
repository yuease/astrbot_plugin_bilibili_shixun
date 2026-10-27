'use strict';
const draftStorage = window.BilibiliShixunTransport.drafts;
const $ = s => document.querySelector(s);
let state = null, detail = null, selectedRun = null, currentView = 'briefing', settingsBase = null, toastTimer, pollTimer, pending = false, detailRequest = 0;
let sourcesBase = null;
const formDirty={settings:false,sources:false,filters:false};
for(const eventName of ['input','change'])document.addEventListener(eventName,event=>{
  const form=event.target.closest?.('form');
  const area={"settings-form":"settings","sources-form":"sources","filters-form":"filters"}[form?.id];
  if(area)formDirty[area]=true;
});
document.addEventListener('click',event=>{
  if(!event.target.closest?.('#add-up,#add-block,[data-remove-row]'))return;
  const form=event.target.closest('form');const area={"sources-form":"sources","filters-form":"filters"}[form?.id];
  if(area)formDirty[area]=true;
});
function syncRemoteSettings(previousVersion){
  if(!previousVersion||previousVersion===state.profile_version||pending)return;
  const area=currentView;if(!Object.hasOwn(formDirty,area))return;
  if(formDirty[area]){
    const message='设置已由Bot或其他窗口更新；你的草稿已保留，请重新载入并核对后再保存。';
    const target=$('#'+area+'-message');if(target){target.textContent=message;const button=document.createElement('button');button.type='button';button.className='text-button';button.dataset.reloadSettings=area;button.textContent='放弃草稿并载入最新设置';target.append(button);}else toast(message,true);
  }else if(area==='settings')fillSettings();else if(area==='sources')fillSources(false);else fillFilters(false);
}
const hasSources = () => state?.capabilities?.sources_v1 === true;
const stableLearning = () => state?.capabilities?.stable_preferences_v1 === true;
const splitList = text => [...new Set(text.split(/[,，\n]/).map(x=>x.trim()).filter(Boolean))];
function activeSources(profile){return profile.sources || {keyword_search:{enabled:true},up_uploads:{enabled:false,uids:[]},following_videos:{enabled:false,uid:''}};}
function draftKey(){return 'bilibili-shixun.sources-draft.v2.'+(state?.user_scope?.key||'unbound')+'.'+(state?.profile?.profile_id||'default');}
function sourceText(article){
  const labels=Array.isArray(article.sources)?article.sources.map(s=>s.label||({keyword_search:'关键词搜索',up_uploads:'UP 投稿',following_videos:'关注动态'}[s.kind])||'订阅来源'):[];
  if(!labels.length&&article.matched_keywords?.length)labels.push(...article.matched_keywords.map(k=>'关键词 · '+k));
  return [...new Set(labels)].map(x=>`<span class="source-label">${esc(x)}</span>`).join('');
}
function cover(article){
  let url='';
  try{const u=new URL(article.cover_url,location.origin);if(article.cover_url&&((u.origin===location.origin&&u.pathname.startsWith('/api/covers/'))||(u.protocol==='https:'&&/\.(hdslb\.com|biliimg\.com)$/.test(u.hostname))))url=u.href;}catch{}
  const text=url?'封面加载中':state?.capabilities?.cover_images_v1?'暂无封面':'封面待接入';
  return `<div class="video-cover"><span class="cover-fallback"><b aria-hidden="true">▻</b><small>${text}</small></span>${url?`<img src="${esc(url)}" alt="${esc(article.title)}的封面" loading="lazy" decoding="async" referrerpolicy="no-referrer">`:''}</div>`;
}
function sourceSummary(){
  const p=state.profile,s=activeSources(p),labels=[];
  if(sourceReady()&&p.source_plan){const v=p.source_plan;if(v.keyword_search.enabled)labels.push(...(p.keywords||[]).map(k=>'搜索 · '+k));const ups=v.ups.filter(x=>x.enabled);if(ups.length)labels.push('指定 UP · '+ups.length+' 位');if(v.following.enabled)labels.push(v.following.scope==='videos'?'关注视频更新':'关注综合动态');$('#keyword-chips').innerHTML=labels.map(k=>`<span class="chip">${esc(k)}</span>`).join('')||'<span class="muted">尚未启用长期来源，可先通过Bot临时搜索。</span>';return;}
  if(s.keyword_search?.enabled)labels.push(...(p.keywords||[]).map(k=>'搜索 · '+k));
  if(s.up_uploads?.enabled)labels.push('UP 投稿 · '+(s.up_uploads.uids||[]).length+' 位');
  if(s.following_videos?.enabled)labels.push('关注动态');
  $('#keyword-chips').innerHTML=labels.map(k=>`<span class="chip">${esc(k)}</span>`).join('')||'<span class="muted">尚未启用来源</span>';
}
function renderAccount(){
 const account=state?.bilibili_account,el=$('#bilibili-account-status');
 if(!el)return;
 const labels={valid:'已登录',expired:'登录已失效',not_logged_in:'尚未登录'};
 el.textContent=account?.qr_pending?'等待 QQ 中的二维码扫码确认。':(labels[account?.status]||'状态待确认')+(account?.uid?' · UID '+account.uid:'')+(!account?.notification_bound?'':account?.notification_mode==='on_use'?' · 登录失效时在下次使用插件时提示':account?.auto_relogin?' · OneBot登录失效时自动提醒':' · 自动提醒关闭');
 if(account?.account_hosting?.allowed===false){el.textContent+='。此档案未获账号托管权限；公开搜索、个人关键词和兴趣可正常使用。';return;}
 if(account?.notification_bound&&!account?.proactive_supported)el.textContent+='。QQ官方机器人在下次使用插件时提示登录失效，不主动推送二维码；请在私聊发送 /B站登录。';
}
function followingStatus(){
  const uid=$('#source-following-uid').value.trim();
  const kind=$('#following-mode')?.value==='dynamics'?'following_dynamics':'following_videos';
  const entry=(state?.source_status||[]).find(s=>s.kind===kind&&s.key===uid);
  const labels={ready:'最近检查可用',configured:'登录配置已提供，尚未验证可用性',missing_credentials:'尚未配置对应账号的登录信息',auth_expired:'登录已过期，请在 QQ 私聊发送 /B站登录',error:'最近采集失败，请检查 B 站登录和网络',unknown:'登录状态尚未检查',disabled:'当前未启用'};
  $('#following-status').textContent=state?.bilibili_account?.account_hosting?.allowed===false?'此档案未获账号托管权限；请联系管理员授权。':kind==='following_dynamics'&&!sourceReady()?'综合动态等待接入；视频来源的检查结果不代表此来源可用':!hasSources()?'等待后端接入；尚未检查登录状态':!uid?'请先在 QQ 私聊发送 /B站登录':labels[entry?.status]||labels.unknown;
}
const statusNames = {collection:'采集完成',completed:'已生成', empty:'暂无推荐', preview:'采集预览', failed:'运行失败', started:'正在采集', collected:'正在处理'};
const feedbackNames = {useful:'有用', not_useful:'没用', later:'暂时不想看'};
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const date = v => {const d = new Date(v);return Number.isNaN(d.getTime())?'时间未知':d.toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',month:'long',day:'numeric',hour:'2-digit',minute:'2-digit',hour12:false});};
const duration = s => s == null ? '时长未知' : s >= 3600 ? `${Math.floor(s/3600)} 小时 ${Math.floor(s%3600/60)} 分` : `${Math.floor(s/60)} 分 ${s%60} 秒`;
const safeUrl = u => {try {const x=new URL(u);return ['http:','https:'].includes(x.protocol)?x.href:'#';}catch{return '#';}};
function toast(message, error=false) { const el=$('#toast'); el.textContent=message; el.classList.toggle('error',error); el.hidden=false; clearTimeout(toastTimer); toastTimer=setTimeout(()=>el.hidden=true,5000); }
async function api(path, body) { return window.BilibiliShixunTransport.api(path, body); }

function setView(view) {
  if(!['briefing','sources','filters','history','preferences','settings'].includes(view)) view='briefing';
  currentView=view; document.querySelectorAll('.view').forEach(x=>x.hidden=x.id!==`view-${view}`);
  document.querySelectorAll('nav [data-view]').forEach(x=>x.classList.toggle('active',x.dataset.view===view));
  const names={briefing:'我的简报',sources:'订阅来源',filters:'过滤与采集',history:'历史记录',preferences:'偏好学习',settings:'兴趣设置'};
  $('#breadcrumb').textContent=`工作台 / ${names[view]}`;
  try{history.replaceState(null,'',`#${view}`);}catch{location.hash=view;}
  if(view==='settings'&&state) fillSettings();
  if(view==='sources'&&state) fillSources();
  if(view==='filters'&&state) fillFilters();
}
function updateButtons(){
 const generating=state?.job?.status==='running',collecting=state?.collection_job?.status==='running',busy=pending||generating||collecting||state?.temporary_search_job?.status==='running';
 $('#generate').disabled=busy;$('#preview').disabled=busy;
 ['save-settings','save-sources','reset-sources','save-filters','reset-filters'].forEach(id=>$('#'+id).disabled=busy);
 document.querySelectorAll('[data-action="generate"]').forEach(b=>b.disabled=busy);
}
function renderJob(){
  const j=state?.job, el=$('#job-panel'); el.hidden=!j||(!['running','failed'].includes(j.status)&&!j.warning);
  if(j?.status==='running') el.innerHTML=`<span class="spinner" aria-hidden="true"></span><div>${j.preview?'正在采集候选内容':'正在生成你的简报'}<small>后台处理期间可以浏览历史。${j.preview?'本次不会调用模型。':'本次通过 AstrBot 模型生成简报，请耐心等待。'}</small></div>`;
  if(j?.status==='failed'||j?.warning) el.innerHTML=`<div>${esc(j.error_message||j.error||j.warning||'运行未完成，暂无详细诊断。')}<small>运行编号：${esc(j.run_id||'尚未分配')} · ${esc(j.error_code||'未记录错误码')}</small>${j.run_id&&j.record_available!==false?`<button class="text-button" data-run="${esc(j.run_id)}">${j.warning?'查看已保存记录':'查看失败记录'} →</button>`:'<small>数据库中未取得本次记录，请检查本地存储；不要用其他运行代替本次记录。</small>'}</div>`;
  updateButtons();
}
async function refresh(loadDetail=true){
  const previousVersion=state?.profile_version;
  const next=await api('/api/state');
  if(state?.user_scope?.key&&state.user_scope.key!==next.user_scope?.key){document.querySelector('main').replaceChildren();location.reload();return;}
  state=next;$('#connection-error').hidden=true;
  syncRemoteSettings(previousVersion);
  sourceSummary();renderPool();renderAccount();
  $('#feedback-count').textContent=state.stats.feedback_count;
  renderHistory();renderPreferences();renderJob();
  if(loadDetail){if(!selectedRun) selectedRun=state.runs.find(r=>r.current_context&&r.status==='completed')?.id || state.runs.find(r=>r.current_context)?.id || state.runs[0]?.id || null;await loadRun(selectedRun);}
  schedulePoll();
}
function schedulePoll(){clearTimeout(pollTimer);if(poolReady()||state?.job?.status==='running')pollTimer=setTimeout(poll,poolReady()?4000:1600);}
async function poll(){
 const oldJob=state?.job,oldCollection=state?.collection_job;
 try{
  await refresh(false);
  if(oldJob?.status==='running'&&state.job?.id===oldJob.id&&state.job.status!=='running'){
   if(state.job.record_available){selectedRun=state.job.run_id;await loadRun(selectedRun);}
   toast(state.job.warning||state.job.error_message||state.job.error||(state.job.preview?'采集完成，候选已保存。':'简报已生成。'),state.job.status==='failed'||!!state.job.warning);
  }
  if(oldCollection?.status==='running'&&state.collection_job?.id===oldCollection.id&&state.collection_job.status!=='running'){
   const c=state.collection_job;
   if(c.record_available&&state.job?.status!=='running')await loadRun(c.run_id);
   toast(c.warning||c.error_message||(c.status==='completed'?'采集完成，候选池已更新；没有调用模型。':'采集未完成，请查看记录。'),c.status==='failed'||!!c.warning);
  }
 }catch(e){$('#connection-error').textContent=e.message;$('#connection-error').hidden=false;pollTimer=setTimeout(poll,5000);}
}
async function loadRun(id){
  const request=++detailRequest;
  const result=id?await api('/api/runs/'+encodeURIComponent(id)):null;
  if(request!==detailRequest)return;
  detail=result;selectedRun=id;renderBriefing();
}
function empty(title, text, action=false){return `<div class="empty"><div class="empty-symbol" aria-hidden="true">▤</div><h2>${esc(title)}</h2><p>${esc(text)}</p>${action?'<button class="button primary" data-action="generate">生成第一份简报</button><button class="button secondary" data-view="settings">先设置我的兴趣</button>':''}</div>`;}
function renderBriefing(){
  const d=detail, download=$('#download');download.hidden=!d?.report||!['completed','empty'].includes(d?.status);
  if(!download.hidden)download.href='/api/runs/'+encodeURIComponent(d.id)+'/download';
  $('#briefing-heading').textContent=d?.operation==='collect'?'采集记录':d?.status==='preview'?'采集预览':d?.status==='failed'?'运行记录':'我的简报';
  $('#briefing-meta').textContent=d?`${date(d.created)} · ${d.picks.length} 条推荐 · ${(d.metrics.candidate_count??d.candidates.length)} 条候选`: '你的下一份简报，从这里开始';
  const regular=d?.picks.filter(x=>x.lane!=='explore').length||0, explore=d?.picks.filter(x=>x.lane==='explore').length||0;
  $('#regular-count').textContent=regular;$('#explore-count').textContent=explore;
  $('#mix-bar').classList.toggle('no-data',!regular&&!explore);
  $('#mix-bar').innerHTML=Array.from({length:regular},()=>'<span></span>').join('')+Array.from({length:explore},()=>'<span class="explore"></span>').join('');
  if(!d){$('#briefing-body').innerHTML=empty('还没有简报，先拾取一点灵感。',poolReady()?'设置来源后先刷新来源，再从候选池生成简报。':'设置兴趣后生成第一份简报。也可以先采集预览，检查 B 站来源是否可用。',true);updateButtons();return;}
  let html='';
  if(!d.current_context)html+='<div class="alert">这份记录来自以前的兴趣设置，可以阅读和导出；反馈请在当前兴趣的简报中提交。</div>';
  const failedSources=(d.source_results||[]).filter(x=>['failed','auth_required','auth_expired'].includes(x.status));
  if(failedSources.length)html+=`<div class="alert">${failedSources.length<d.source_results.length?'部分来源采集失败；其他来源已继续处理。':'全部来源采集失败。'}${failedSources.map(x=>`<div>${esc(x.label||x.kind)}：${esc(x.error_message||'旧记录未保存详细原因。')}</div>`).join('')}</div>`;
  else if(d.failures.length)html+=`<div class="alert">${d.failures.map(esc).join('；')}</div>`;
  if(d.source_results?.some(x=>x.status==='pending'))html+='<div class="alert">本次仍有来源尚未请求，不能据此判断是否有更新。</div>';
  if(d.source_results?.length)html+=`<details class="source-results"><summary>本次来源情况 · ${d.source_results.length} 个订阅</summary>${d.source_results.map(x=>`<div class="source-result"><span>${esc(x.label||x.kind)}</span><span>${esc(({ok:'采集成功',empty:'暂无更新',pending:'尚未请求',failed:'采集失败',auth_required:'需要登录',auth_expired:'登录已过期'}[x.status])||'状态未知')} · ${Number.isInteger(x.count)?x.count:0} 条${x.batches!=null?' · '+Number(x.batches)+' 批':''}${x.stop_reason?'<br>'+esc(({single_batch:'此路由仅一批',empty_batch:'本批为空',missing_cursor:'缺少可用游标',cursor_not_advanced:'游标未推进',no_new_items:'本批没有新 ID',time_boundary:'已达时间下界',batch_limit:'达到每位 UP 批次上限',global_request_budget:'全局请求预算已用完',global_time_budget:'全局时间预算已用完',request_failed:'请求失败，未判定为末尾'}[x.stop_reason])||x.stop_reason):''}${x.error_message?`<br>${esc(x.error_message)}${x.error_code?` [${esc(x.error_code)}]`:''}`:''}</span></div>`).join('')}</details>`;
  if(d.preference_update_error)html+='<div class="alert">后台偏好暂未更新，已使用最近的有效版本。</div>';
  if(d.status==='failed'||d.export_failed)html+=`<div class="alert ${d.status==='failed'?'error':''}">${esc(d.error_message||'旧记录未保存详细原因。')}<br><small>阶段：${esc(d.failed_stage||'未知')}；类型：${esc(d.error_type||'未知')}；错误码：${esc(d.error_code||'未记录')}</small></div>`;
  if(d.picks.length)html+=d.picks.map(card).join('');
  else if(d.status==='empty')html+=d.empty_reason==='pool_empty'?empty('候选池暂时没有可用内容。','可先刷新来源，或检查当前来源、过滤、有效期与推荐冷却设置。本次没有调用模型。'):empty('这一轮，没有合适的推荐。','可能是近期已推荐、时长限制或内容不匹配。我们保留空缺，不用无关内容填满简报。');
  else if(d.operation==='collect')html+='<p class="candidate-info">以下为本次已保存条目，规则命中只作标记；生成时按当前来源、过滤、时效和冷却重新筛选。本次没有调用模型。</p>';
  else if(d.status==='preview')html+='<p class="candidate-info">以下是采集和过滤后的候选，尚未经过模型推荐。本次没有调用模型，也不会占用推荐历史。</p>';
  if(!d.picks.length&&d.candidates.length)html+=d.candidates.map(candidateCard).join('');
  if(!d.picks.length&&!d.candidates.length&&d.status!=='empty')html+=empty(d.empty_reason==='sources_empty'?'来源暂未返回内容':d.empty_reason==='filtered_empty'?'本轮候选已被过滤':'暂无可显示内容',d.empty_reason==='sources_empty'?'来源请求成功，但这次没有返回可用条目。':d.empty_reason==='filtered_empty'?'已取得内容都处于推荐冷却期或不满足时长要求，可查看运行信息。':d.empty_reason==='pool_empty'?'候选池暂无符合当前设置的内容，可先刷新来源，再查看过滤与采集设置。':'请查看本次运行和来源的诊断说明。');
  const m=d.metrics;
  html+=`<details class="run-diagnostics"><summary>查看运行信息</summary><ul><li>运行编号：${esc(d.id)}</li><li>状态：${esc(statusNames[d.status]||d.status)}</li><li>逻辑模型调用：${m.logical_model_calls??'未知'} 次</li><li>耗时：${Number.isFinite(m.elapsed_seconds)?m.elapsed_seconds.toFixed(1)+' 秒':'未知'}</li><li>近期推荐过滤：${m.history_filtered??0} 条；时长过滤：${m.duration_filtered??0} 条</li><li>模型：${esc(m.model_name||'未记录')}</li></ul></details>`;
  if(d.error_details)html+=`<details class="run-diagnostics"><summary>查看安全诊断详情</summary><pre>${esc(JSON.stringify(d.error_details,null,2))}</pre></details>`;
  $('#briefing-body').innerHTML=html;updateButtons();
}
function typeText(a){return ({video:'视频',text:'文字动态',image:'图文动态',article:'文章动态',audio:'音频动态',live:'直播动态',unknown:'类型未知'}[a.content_type||'video'])||'类型未知';}
function contentTime(a){return (a.content_type||'video')==='video'?duration(a.duration_seconds):typeText(a)+' · 无视频时长';}
function contentMedia(a){
 if((a.content_type||'video')==='video')return cover(a);
 const images=(a.images||[]).filter(v=>{try{const u=new URL(v);return u.protocol==='https:'&&/\.(hdslb\.com|biliimg\.com)$/.test(u.hostname)&&!u.username&&!u.password;}catch{return false;}}).slice(0,4);
 return `<div class="dynamic-images">${images.length?images.map((u,i)=>`<div class="dynamic-image"><span>图片加载失败</span><img src="${esc(u)}" alt="动态配图 ${i+1}，图片内容未分析" loading="lazy" referrerpolicy="no-referrer"></div>`).join(''):'<div class="dynamic-image missing">暂无图片</div>'}</div>`;
}
function dynamicContext(a){return (a.filter_hits||[]).map(h=>`<p class="field-note">规则标记 ${esc(h.id)} v${Number(h.version)}：${esc(h.reason)}</p>`).join('')+( (a.dynamic_contexts||[]).length?`<details class="muted"><summary>动态上下文与原文</summary>${a.dynamic_contexts.map(c=>`<p>${esc(c.author||'作者未知')}${c.original_author_uid?' · 原作者 UID '+esc(c.original_author_uid):''}：${esc(c.text||'B 站未提供文字')} <a href="${esc(safeUrl(c.url))}" target="_blank" rel="noopener noreferrer">打开动态原文 ↗</a></p>`).join('')}</details>`:'');}
function candidateCard(a,i){return `<article class="candidate-card"><div class="video-heading">${contentMedia(a)}<div class="video-heading-copy"><div class="card-source">候选 ${i+1} · ${esc(a.author)} · ${contentTime(a)}</div><h3><a href="${esc(safeUrl(a.url))}" target="_blank" rel="noopener noreferrer">${esc(a.title)} ↗</a></h3><div class="source-labels">${sourceText(a)}<span class="source-label">${esc(typeText(a))}</span></div></div></div><p class="card-summary">${esc(a.excerpt||((a.content_type||'video')==='video'?'来源未提供简介。':'B 站未提供可用文字；图片仅展示，请打开原文查看。'))}</p>${dynamicContext(a)}<div class="card-tags">${(a.tags||[]).slice(0,8).map(x=>`<span># ${esc(x)}</span>`).join('')}</div></article>`;}
function card(p){const a=p.article,f=p.feedback,isExplore=p.lane==='explore';return `<article class="brief-card" data-rec="${esc(p.recommendation_id)}"><div class="card-top"><span class="card-number">${String(p.position).padStart(2,'0')}</span><span class="badge ${isExplore?'explore':''}">${isExplore?'探索推荐':'为你精选'}</span><span class="card-time">◷ ${contentTime(a)}</span></div><div class="video-heading">${contentMedia(a)}<div class="video-heading-copy"><h3><a href="${esc(safeUrl(a.url))}" target="_blank" rel="noopener noreferrer">${esc(a.title)}</a></h3><div class="card-source"><span>${esc(a.author||'未知作者')}</span><span>${a.published_at?date(a.published_at):esc(a.published||'时间未知')}</span></div><div class="source-labels">${sourceText(a)}<span class="source-label">${esc(typeText(a))}</span></div></div></div><p class="card-summary">${esc(p.summary)}</p><div class="reason"><strong>${isExplore?'探索理由':'为什么推荐'}</strong><span>${esc(isExplore?p.exploration_reason||p.reason:p.reason)}</span></div>${dynamicContext(a)}<div class="card-tags">${(a.tags||[]).slice(0,5).map(x=>`<span># ${esc(x)}</span>`).join('')}</div>${p.action?`<details class="muted"><summary>建议行动</summary>${esc(p.action)}</details>`:''}<div class="card-bottom">${Object.entries(feedbackNames).map(([v,n])=>`<button class="feedback-btn ${f?.value===v?'selected':''}" data-feedback="${v}" ${!detail.current_context?'disabled':''} aria-pressed="${f?.value===v}">${v==='useful'?'＋ ':v==='later'?'◷ ':''}${n}</button>`).join('')}${f&&detail.current_context?`<button class="text-button" data-undo="${esc(f.id)}">撤销</button>`:''}<a class="open-source" href="${esc(safeUrl(a.url))}" target="_blank" rel="noopener noreferrer">打开原文 ↗</a></div>${f?`<div class="feedback-state">已记录：${esc(feedbackNames[f.value])}${f.reason?' · '+esc(f.reason):''}</div>`:''}${f?.value==='not_useful'&&detail.current_context?`<div class="feedback-reason"><label>原因可选 <select data-reason><option value="">不补充原因</option>${['不感兴趣','太浅','太深','已经知道','质量不好','太长','太短'].map(x=>`<option value="${x}" ${f.reason===x?'selected':''}>${x}</option>`).join('')}</select></label><span>不需要额外确认，后台会自动处理。</span></div>`:''}</article>`;}
function renderHistory(){
  $('#history-body').innerHTML=state.runs.length?`<p class="candidate-info">最近 ${state.runs.length} 次运行 · 点击一条记录查看详情</p><div class="history-list">${state.runs.map(r=>`<button class="history-row" data-run="${esc(r.id)}"><span class="history-status ${r.status==='failed'?'failed':''}">${esc(statusNames[r.status]||r.status)}</span><span class="run-info"><strong>${date(r.created)}${!r.current_context?'<span class="old-label">历史兴趣</span>':''}</strong><small>${esc((r.source_labels?.length?r.source_labels:r.keywords||[]).join(' / ')||'未记录来源')} · ${esc(r.id)}</small></span><span class="history-count">${r.count} 推荐 / ${r.candidates} 候选</span><span class="history-arrow">↗</span></button>`).join('')}</div>`:empty('还没有运行记录','第一次采集或生成后，结果会出现在这里。');
}
function renderPreferences(){
  const s=state.stats, prefs=state.preferences, entries=Object.entries(prefs.weights),enabled=state.profile.learning.enabled;
  const name=k=>k.startsWith('tag:')?k.slice(4):k.startsWith('duration:')?'时长区间 '+k.slice(9):k;
  $('#preferences-body').innerHTML=`<div class="stats-grid"><div class="stat-tile"><span>有效反馈</span><strong>${s.feedback_count}</strong><small>包含暂时不想看</small></div><div class="stat-tile"><span>已评价内容的有用率</span><strong>${s.useful_rate==null?'—':Math.round(s.useful_rate*100)+'%'}</strong><small>基于 ${s.evaluated} 条有用 / 没用评价</small></div><div class="stat-tile"><span>已激活偏好</span><strong>${entries.length}</strong><small>${enabled?'自动学习已开启':'当前不应用学习权重'}</small></div></div><section class="preference-section"><div class="section-heading"><h2>当前偏好</h2><span class="muted">${prefs.version?'版本 '+prefs.version:'尚未生成版本'}</span></div>${!enabled?'<div class="alert">自动学习的排序影响已关闭。反馈仍会保留，后端仍可能汇总证据。</div>':''}${entries.length?entries.map(([k,v])=>`<div class="weight-row"><span>${esc(name(k))}</span><span>${v>0?'适当增加':'适当减少'} · ${v>0?'+':''}${Number(v).toFixed(2)}</span></div>`).join(''):'<p class="muted">还没有足够证据形成稳定偏好，这是正常的。不同日期、不同内容上的一致反馈积累后，系统才会小幅调整推荐。</p>'}<p class="field-note">${stableLearning()?'偏好随当前兴趣档案保留，不因切换关键词或来源而消失。':'当前后端仍按关键词等条件隔离偏好；切换后旧证据保留，但不会合并显示。'}探索推荐不应用这些调整。</p></section><div class="principles"><div><h3>一次反馈，不急着下结论</h3><p>限制同一天连续反馈的影响，避免一时疲惫改变长期推荐。</p></div><div><h3>能纠正，也会淡化</h3><p>相反反馈抵消旧证据；撤销后重新计算，较早的证据逐渐减弱。</p></div><div><h3>明确兴趣，始终优先</h3><p>只有限调整常规排序，不生成永久黑名单。你可以随时修改兴趣设置。</p></div></div><p class="field-note">反馈率与有用率受主动评价习惯影响，不代表所有推荐的真实准确率。当前累计推荐 ${s.recommendations} 条。</p>`;
}
function fillSettings(){
  formDirty.settings=false;
  const p=state.profile;settingsBase=structuredClone(p);
  $('#keywords').value=(p.keywords||[]).join(', ');$('#legacy-keywords').hidden=hasSources();
  $('#learning-context-note').textContent=stableLearning()?'同一目标的文字调整延续学习；明确切换目标时隔离旧学习。关键词、订阅来源和临时时长不重置学习。':'当前后端仍按关键词等条件隔离偏好，旧记录不会删除。稳定兴趣档案正在等待接入。';
  $('#interest-mode').value='same_goal';
  $('#interests').value=p.interests;$('#max-picks').value=p.max_picks;
  $('#max-duration').value=p.max_duration_seconds==null?'':p.max_duration_seconds/60;
  $('#exploration').value=p.exploration_ratio*100;$('#cooldown').value=p.history_cooldown_days;
  $('#learning').checked=p.learning.enabled;$('#settings-message').textContent='';updateButtons();
}
async function startRun(preview=false){if(pending)return;pending=true;updateButtons();try{const j=await api(poolReady()?(preview?'/api/collect':'/api/generate'):'/api/run',poolReady()?{}:{preview});if(poolReady()&&preview){state.collection_job=j;}else{state.job=j;}setView('briefing');renderJob();renderPool();schedulePoll();}catch(e){toast(e.message,true);}finally{pending=false;updateButtons();}}
async function sendFeedback(button, value, reason='', undo=null){const cardEl=button.closest('[data-rec]');if(!cardEl||cardEl.dataset.busy)return;cardEl.dataset.busy='true';cardEl.querySelectorAll('button,select').forEach(x=>x.disabled=true);try{const payload=undo?{event:undo,key:crypto.randomUUID()}:{recommendation:cardEl.dataset.rec,value,reason,key:crypto.randomUUID()};const r=await api(undo?'/api/undo':'/api/feedback',payload);await refresh();toast(r.warning||(undo?'已撤销，偏好证据已重新计算。':'反馈已记录，后台自动更新。'));}catch(e){toast(e.message,true);cardEl.querySelectorAll('button,select').forEach(x=>x.disabled=false);}finally{delete cardEl.dataset.busy;}}
document.addEventListener('click',async e=>{const nav=e.target.closest('[data-view]');if(nav){setView(nav.dataset.view);return;}const run=e.target.closest('[data-run]');if(run){try{await loadRun(run.dataset.run);setView('briefing');window.scrollTo({top:0,behavior:'smooth'});}catch(err){toast(err.message,true);}return;}const fb=e.target.closest('[data-feedback]');if(fb){await sendFeedback(fb,fb.dataset.feedback);return;}const undo=e.target.closest('[data-undo]');if(undo){await sendFeedback(undo,null,'',undo.dataset.undo);return;}if(e.target.closest('[data-action="generate"]'))startRun(false);});
document.addEventListener('change',e=>{if(e.target.matches('[data-reason]'))sendFeedback(e.target,'not_useful',e.target.value);});
document.addEventListener('click',event=>{const button=event.target.closest?.('[data-reload-settings]');if(!button)return;const area=button.dataset.reloadSettings;if(area==='settings')fillSettings();else if(area==='sources')fillSources(false);else if(area==='filters')fillFilters(false);});
$('#generate').addEventListener('click',()=>startRun(false));$('#preview').addEventListener('click',()=>startRun(true));
$('#refresh-history').addEventListener('click',async()=>{try{await refresh(false);toast('历史记录已刷新。');}catch(e){toast(e.message,true);}});
$('#source-following-uid').addEventListener('input',followingStatus);
$('#reset-sources').addEventListener('click',()=>{
  try{draftStorage.removeItem(localKey('sources'));}catch{}
  fillSources(false);$('#sources-message').textContent='已恢复页面载入时的已生效配置。';
});
document.addEventListener('error',e=>{if(e.target.matches?.('.dynamic-image img')){e.target.hidden=true;e.target.parentElement.classList.add('failed');}if(e.target.matches?.('.video-cover img')){e.target.hidden=true;e.target.closest('.video-cover').querySelector('small').textContent='封面加载失败';}},true);
document.addEventListener('load',e=>{if(e.target.matches?.('.dynamic-image img'))e.target.parentElement.classList.add('loaded');if(e.target.matches?.('.video-cover img'))e.target.closest('.video-cover').classList.add('loaded');},true);
$('#settings-form').addEventListener('submit',async e=>{e.preventDefault();if(pending)return;pending=true;updateButtons();try{const raw=$('#max-duration').value;const changes={keywords:hasSources()?(settingsBase.keywords||[]):splitList($('#keywords').value),interests:$('#interests').value,max_picks:Number($('#max-picks').value),max_duration_seconds:raw===''?null:Math.round(Number(raw)*60),exploration_ratio:Number($('#exploration').value)/100,history_cooldown_days:Number($('#cooldown').value),learning_enabled:$('#learning').checked};const r=await api('/api/profile',{expected:settingsBase,changes,interest_mode:$('#interest-mode').value,request_id:crypto.randomUUID()});await refresh(false);fillSettings();$('#settings-message').textContent=r.changed_context?'设置已保存，已启用新的学习上下文。':'设置已保存，下次运行生效。';if(r.changed_context){selectedRun=null;await loadRun(state.runs.find(x=>x.current_context)?.id||null);}toast('兴趣设置已保存。');}catch(err){$('#settings-message').textContent=err.message;toast(err.message,true);}finally{pending=false;updateButtons();}});
// Versioned capabilities prevent draft settings from changing legacy behavior.
const sourceReady=()=>state?.capabilities?.sources_v2===true;
const filterReady=()=>state?.capabilities?.filter_rules_v1===true;
const poolReady=()=>state?.capabilities?.candidate_pool_v1===true;
let filterBase=null, loadedRules=[];
function localKey(area){return `bilibili-shixun.${area}.v3.${state?.user_scope?.key||'unbound'}.${state?.profile?.profile_id||'default'}`;}
function readDraft(area){try{return JSON.parse(draftStorage.getItem(localKey(area))||'null');}catch{return null;}}
function defaultPlan(){const s=activeSources(state.profile);return {keyword_search:{enabled:!!s.keyword_search?.enabled},ups:(s.up_uploads?.uids||[]).map(uid=>({uid,enabled:!!s.up_uploads.enabled,scope:'uploads',content_type:'all'})),following:{enabled:!!s.following_videos?.enabled,uid:s.following_videos?.uid||'',scope:'videos',content_type:'all'}};}
function options(values,current){return values.map(([v,t])=>`<option value="${v}" ${v===current?'selected':''}>${t}</option>`).join('');}
function addUp(row={uid:'',enabled:true,scope:'uploads',content_type:'all'}){
  if($('#up-rows').children.length>=10){toast('最多添加 10 位 UP。',true);return;}
  const el=document.createElement('div');el.className='editor-row up-row';
  el.innerHTML=`<div class="row-title"><label><input type="checkbox" data-up-enabled ${row.enabled?'checked':''}> 启用此 UP</label><button type="button" class="text-button" data-remove-row>移除</button></div><div class="up-grid"><label>UP 主 UID<input data-up-uid inputmode="numeric" maxlength="30" value="${esc(row.uid)}" placeholder="数字 UID" required></label><label>订阅范围<select data-up-scope>${options([['uploads','视频投稿'],['dynamics','动态'],['both','投稿＋动态']],row.scope)}</select></label><label>动态内容类型<select data-up-type>${options([['all','全部动态'],['video','仅视频'],['non_video','非视频动态']],row.content_type)}</select></label></div>`;
  $('#up-rows').append(el);syncTypes();
}
function syncTypes(){document.querySelectorAll('.up-row').forEach(el=>el.querySelector('[data-up-type]').disabled=el.querySelector('[data-up-scope]').value==='uploads');$('#following-type').disabled=$('#following-mode').value==='videos';}
function fillSources(useDraft=true){
  formDirty.sources=false;
  sourcesBase=structuredClone(state.profile);
  const draft=useDraft?readDraft('sources'):null;
  const v=draft||{keywords:state.profile.keywords||[],source_plan:state.profile.source_plan||defaultPlan()},s=v.source_plan;
  $('#source-keyword-enabled').checked=!!s.keyword_search.enabled;$('#source-keywords').value=v.keywords.join(', ');
  $('#up-rows').innerHTML='';s.ups.forEach(addUp);
  $('#source-following-enabled').checked=!!s.following.enabled;$('#source-following-uid').value=s.following.uid;
  $('#following-mode').value=s.following.scope;$('#following-type').value=s.following.content_type;syncTypes();
  $('#sources-notice').textContent=sourceReady()?'来源在保存后生效；当前页面内存草稿不会自动应用。':'新版动态订阅等待后端接入。此页保存的是当前页面内存草稿，首页仍显示实际生效的来源。';
  $('#save-sources').textContent=sourceReady()?'保存来源设置':'保存来源草稿';$('#sources-message').textContent=draft?'已载入当前页面内存草稿，尚未保存；刷新即丢失。':'';
  followingStatus();updateButtons();
}
function addBlock(row={uid:'',scope:'dynamics'}){
 const el=document.createElement('div');el.className='editor-row block-row';el.innerHTML=`<div class="block-grid"><label>屏蔽 UP 的 UID<input data-block-uid inputmode="numeric" maxlength="30" value="${esc(row.uid)}" required></label><label>屏蔽范围<select data-block-scope>${options([['dynamics','仅屏蔽动态'],['all','屏蔽全部内容']],row.scope)}</select></label><button type="button" class="text-button" data-remove-row>移出名单</button></div>`;$('#block-rows').append(el);
}
function fillFilters(useDraft=true){
 formDirty.filters=false;
 filterBase=structuredClone(state.profile);const draft=useDraft?readDraft('filters'):null;
 const v=draft||{filters:state.profile.filters||{rules:[{id:'lottery',enabled:true}],blocked_ups:[]},collection:state.profile.collection||{enabled:false,interval_minutes:60,dynamic_ttl_hours:72}};
 loadedRules=structuredClone(v.filters.rules);
 const catalog=[...(state.filter_rule_catalog||[{id:'lottery',label:'过滤抽奖动态',description:'仅过滤明确的抽奖内容，不因正文提到“抽奖”就直接排除。'}])];
 const ids=new Set(catalog.map(x=>x.id));loadedRules.filter(x=>!ids.has(x.id)).forEach(x=>catalog.push({id:x.id,label:x.id,description:'后端未加载此扩展规则，配置保留但不会执行。'}));
 $('#rule-list').innerHTML=catalog.map(r=>`<div class="toggle-row"><div><h3>${esc(r.label)}</h3><p>${esc(r.description)}</p></div><label class="switch"><input type="checkbox" data-rule="${esc(r.id)}" aria-label="${esc(r.label)}" ${loadedRules.find(x=>x.id===r.id)?.enabled?'checked':''}><span></span></label></div>`).join('');
 $('#block-rows').innerHTML='';v.filters.blocked_ups.forEach(addBlock);
 $('#schedule-enabled').checked=v.collection.enabled;$('#interval-minutes').value=String(v.collection.interval_minutes);$('#dynamic-ttl').value=String(v.collection.dynamic_ttl_hours);
 const ready=filterReady()&&poolReady();$('#save-filters').textContent=ready?'保存并应用设置':'保存设置草稿';
 $('#filters-notice').textContent=ready?'规则在下一次筛选时应用；调整规则不会删除采集内容。':'过滤规则与定时采集等待后端接入。当前仅保存当前页面内存草稿，不会实际屏蔽内容或启动定时任务。';
 $('#filters-message').textContent=draft?'已载入当前页面内存草稿，尚未保存；刷新即丢失。':state.filter_warnings?.length?'部分扩展规则或版本尚未加载，其配置保留但不会执行。':'';
 $('#filter-counts').textContent=state.pool?.filter_counts?'当前规则命中：'+Object.entries(state.pool.filter_counts).map(([k,n])=>`${catalog.find(x=>x.id===k)?.label||k} ${n} 条`).join('；'):'命中统计尚未提供；不会把未知数量显示成零。';
}
function renderPool(){
 const ready=poolReady(),p=state.pool,c=state.collection_job,g=state.job;
 $('#preview').textContent=ready?'刷新来源':'仅采集预览';
 $('#pool-status').innerHTML=`<section><span class="eyebrow">COLLECTION</span><h3>${ready?(c?.status==='running'?'正在采集':c?.status==='failed'?'采集失败':'来源采集'):'独立采集待接入'}</h3><p>${esc(ready?(c?.warning||c?.error_message||(p?.last_collected_at?'上次采集 '+date(p.last_collected_at):'尚未采集')):'现有预览仍可用，不调用模型。')}</p>${ready&&c?.record_available?`<button class="text-button" data-run="${esc(c.run_id)}">查看本次采集记录 ↗</button>`:''}</section><section><span class="eyebrow">CANDIDATE POOL</span><h3>${ready&&Number.isInteger(p?.eligible_count)?p.eligible_count+' 条有效候选':ready?'候选数量待更新':'候选池待接入'}</h3><p>${ready?'按当前设置计算，未截取候选预算；生成时固定本次快照。':'当前页面内存草稿不会改变实际采集或筛选。'}</p></section><section><span class="eyebrow">BRIEFING</span><h3>${g?.status==='running'&&!g.preview?'正在生成简报':'按需生成简报'}</h3><p>${ready?'从候选池生成简报，复用 AstrBot 模型；普通聊天检索不额外调用模型。':'当前生成仍先采集，再调用模型。'}</p></section>`;
 if(ready){$('#preview').disabled=pending||c?.status==='running'||g?.status==='running';$('#generate').disabled=pending||c?.status==='running'||g?.status==='running';}
 const temporary=state.temporary_search_job;
 if(temporary){const title=temporary.results_expired?'临时结果已过期':temporary.status==='running'?'正在临时搜索':temporary.status==='completed'?(temporary.result_count?'临时搜索已有结果':'临时搜索未匹配到内容'):'临时搜索未完成';$('#pool-status').insertAdjacentHTML('beforeend',`<section><span class="eyebrow">TEMPORARY SEARCH</span><h3>${title}</h3><p>${esc(temporary.error_message||(temporary.results_expired?'请通过Bot重新搜索。':(temporary.keyword?'关键词：'+temporary.keyword+'。':'')+(temporary.status==='completed'?'本次返回 '+temporary.result_count+' 条；':'')+'没有写入长期订阅。'))}</p></section>`);}
 updateButtons();
}
$('#add-up').addEventListener('click',()=>addUp());$('#add-block').addEventListener('click',()=>addBlock());
document.addEventListener('click',e=>{const b=e.target.closest('[data-remove-row]');if(b)b.closest('.editor-row').remove();});
document.addEventListener('change',e=>{if(e.target.matches('[data-up-scope],#following-mode')){syncTypes();followingStatus();}});
$('#reset-filters').addEventListener('click',()=>{try{draftStorage.removeItem(localKey('filters'));}catch{}fillFilters(false);});
$('#sources-form').addEventListener('submit',async e=>{
 e.preventDefault();if(pending||!state)return;
 const keywords=splitList($('#source-keywords').value),ups=[...document.querySelectorAll('.up-row')].map(el=>({uid:el.querySelector('[data-up-uid]').value.trim(),enabled:el.querySelector('[data-up-enabled]').checked,scope:el.querySelector('[data-up-scope]').value,content_type:el.querySelector('[data-up-type]').value}));
 const source_plan={keyword_search:{enabled:$('#source-keyword-enabled').checked},ups,following:{enabled:$('#source-following-enabled').checked,uid:$('#source-following-uid').value.trim(),scope:$('#following-mode').value,content_type:$('#following-type').value}};
 if(keywords.length>5||(source_plan.keyword_search.enabled&&!keywords.length)){toast('启用搜索时填写 1–5 个关键词。',true);return;}
 if(ups.some(x=>!/^\d{1,30}$/.test(x.uid))||new Set(ups.map(x=>x.uid)).size!==ups.length){toast('UP UID 须为数字且不能重复。',true);return;}
 const f=source_plan.following;if((f.uid||f.enabled)&&!/^\d{1,30}$/.test(f.uid)){toast('请填写有效的登录账号 UID。',true);return;}
 await savePlan('sources',{keywords,source_plan},sourceReady(),sourcesBase);
});
$('#filters-form').addEventListener('submit',async e=>{
 e.preventDefault();if(pending||!state)return;
 const blocked_ups=[...document.querySelectorAll('.block-row')].map(el=>({uid:el.querySelector('[data-block-uid]').value.trim(),scope:el.querySelector('[data-block-scope]').value}));
 if(blocked_ups.some(x=>!/^\d{1,30}$/.test(x.uid))||new Set(blocked_ups.map(x=>x.uid)).size!==blocked_ups.length){toast('屏蔽 UID 须为数字且不能重复。',true);return;}
 const rules=[...document.querySelectorAll('[data-rule]')].map(el=>({...loadedRules.find(x=>x.id===el.dataset.rule),id:el.dataset.rule,enabled:el.checked}));
 const collection={...(filterBase.collection||{}),enabled:$('#schedule-enabled').checked,interval_minutes:Number($('#interval-minutes').value),dynamic_ttl_hours:Number($('#dynamic-ttl').value)};
 await savePlan('filters',{filters:{...(filterBase.filters||{}),rules,blocked_ups},collection},filterReady()&&poolReady(),filterBase);
});
async function savePlan(area,changes,ready,base){
 if(!ready){try{draftStorage.setItem(localKey(area),JSON.stringify(changes));$('#'+area+'-message').textContent='草稿仅保存在当前页面内存，尚未保存；刷新即丢失。';toast('草稿已保存，实际配置未改变。');}catch{toast('当前页面无法保存内存草稿。',true);}return;}
 pending=true;updateButtons();$('#save-filters').disabled=true;
 try{await api('/api/profile',{expected:base,changes});draftStorage.removeItem(localKey(area));await refresh(false);area==='sources'?fillSources(false):fillFilters(false);toast('设置已应用。');}catch(e){toast(e.message,true);}finally{pending=false;updateButtons();}
}

$('#today').textContent=new Date().toLocaleDateString('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'long',day:'numeric',weekday:'long'});
(async()=>{try{await window.BilibiliShixunTransport.ready();setView(location.hash.slice(1));await refresh();if(currentView==='settings')fillSettings();if(currentView==='sources')fillSources();if(currentView==='filters')fillFilters();}catch(e){$('#connection-error').textContent=e.message;$('#connection-error').hidden=false;$('#briefing-body').innerHTML=empty('暂时无法读取数据',e.message);pollTimer=setTimeout(poll,5000);}})();
