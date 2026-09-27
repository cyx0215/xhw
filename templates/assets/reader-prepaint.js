(function(){
var r=document.documentElement, book=document.currentScript.dataset.book;
try{
  var t=localStorage.getItem('xhw-theme');
  if(['day','sepia','green','night','black'].indexOf(t)<0)
    t=matchMedia('(prefers-color-scheme: dark)').matches?'night':'day';
  r.dataset.theme=t;
  r.dataset.tone=(t==='night'||t==='black')?'dark':'light';
}catch(e){}
try{
  var st=JSON.parse(localStorage.getItem('xhw-pos-'+book)||'{}');
  if(st.scale) r.style.setProperty('--font-scale',st.scale);
}catch(e){}
// 全站阅读偏好（行距 / 字体），首次绘制前钉上，避免跳变
try{
  var pf=JSON.parse(localStorage.getItem('xhw-prefs')||'{}');
  if(pf.lh) r.dataset.lh=pf.lh;
  if(pf.font) r.dataset.font=pf.font;
}catch(e){}
if(matchMedia('(min-width:1080px)').matches) r.dataset.toc='on';
})();