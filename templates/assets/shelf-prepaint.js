(function(){
var r=document.documentElement;
try{
  var t=localStorage.getItem('xhw-theme');
  if(['day','sepia','green','night','black'].indexOf(t)<0)
    t=matchMedia('(prefers-color-scheme: dark)').matches?'night':'day';
  r.dataset.theme=t;
  r.dataset.tone=(t==='night'||t==='black')?'dark':'light';
}catch(e){}
})();