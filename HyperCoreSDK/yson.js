var yson = {};
var sd = 0;
yson.parseAsync = function(text, cb, r) {
  var u;
  try {
    r = JSON.parse(text);
    if(cb){ cb(u, r); }
  } catch(e) {
    if(cb){ cb(e); }
  }
  return r;
};
yson.stringifyAsync = function(v, cb, r) {
  try {
    r = JSON.stringify(v);
  } catch(e) {
    r = '' + v;
  }
  if(cb){
    sd++;
    if(sd > 1){
      var rr = r;
      var cc = cb;
      sd--;
      queueMicrotask(function(){ cc(rr); });
    } else {
      cb(r);
      sd--;
    }
  }
  return r;
};
JSON.parseAsync = yson.parseAsync;
JSON.stringifyAsync = yson.stringifyAsync;
if(typeof module !== 'undefined') { module.exports = yson; }