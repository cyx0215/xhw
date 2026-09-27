
var TK='xhw-theme';
var root=document.documentElement;
// 背景主题：浅色系 day/sepia/green，深色系 night/black
var THEMES=['day','sepia','green','night','black'];
var DARK={night:1,black:1};
function readTheme(){
  try{ var v=localStorage.getItem(TK); if(THEMES.indexOf(v)>=0) return v; }catch(e){}
  return matchMedia('(prefers-color-scheme: dark)').matches?'night':'day';
}
function paintTheme(t){
  if(THEMES.indexOf(t)<0) t='day';
  root.dataset.theme=t;
  root.dataset.tone=DARK[t]?'dark':'light';
  // 书架的单按钮：显示「切到另一色系」
  var b=document.getElementById('theme');
  if(b) b.textContent=(DARK[t]?'日间':'夜间');
  document.querySelectorAll('[data-set-theme]').forEach(function(x){
    x.setAttribute('aria-pressed', x.dataset.setTheme===t?'true':'false');
  });
}
paintTheme(readTheme());
function setTheme(t){
  try{
    localStorage.setItem(TK,t);
    // 记住每个色系最后用的是哪个，书架单按钮来回切时还原
    localStorage.setItem(TK+'-'+(DARK[t]?'dark':'light'),t);
  }catch(e){}
  // 支持时用 View Transitions 做交叉淡化
  if(document.startViewTransition && !matchMedia('(prefers-reduced-motion: reduce)').matches){
    document.startViewTransition(function(){ paintTheme(t); });
  }else{ paintTheme(t); }
}
function toggleTheme(){
  var toDark=!DARK[root.dataset.theme];
  var t=toDark?'night':'day';
  try{
    var last=localStorage.getItem(TK+'-'+(toDark?'dark':'light'));
    if(THEMES.indexOf(last)>=0 && !!DARK[last]===toDark) t=last;
  }catch(e){}
  setTheme(t);
}
// 观察器：元素进入视口后加 .in，一次性触发不回退。
// 构造或调用失败（旧浏览器 polyfill 缺陷等）时退化为直接全部显示，
// 避免因为动画层出错而导致正文停留在 opacity:0 的不可见状态。
var reveal=(function(){
  var showAll=function(els){ [].forEach.call(els,function(e){ e.classList.add('in'); }); };
  if(!window.IntersectionObserver || matchMedia('(prefers-reduced-motion: reduce)').matches){
    return showAll;
  }
  try{
    var io=new IntersectionObserver(function(es){
      es.forEach(function(e){
        if(e.isIntersecting){ e.target.classList.add('in'); io.unobserve(e.target); }
      });
    },{rootMargin:'0px 0px -12% 0px',threshold:.02});
    return function(els){
      try{ [].forEach.call(els,function(e){ io.observe(e); }); }
      catch(e){ showAll(els); }
    };
  }catch(e){ return showAll; }
})();
