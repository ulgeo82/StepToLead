const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const ts=require('typescript');
const postcss=require('postcss');
const element=(type,props)=>({type,props});
function render(file,states){
  let hook=0;
  const exports={};
  const source=fs.readFileSync(path.join(__dirname,'../components',file),'utf8');
  const js=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;
  vm.runInNewContext(js,{exports,require(name){
    if(name==='react')return {useEffect(){},useRef(){return {current:{}}},useState(initial){const i=hook++;return [i in states?states[i]:initial,()=>{}]}};
    if(name==='react/jsx-runtime')return {jsx:element,jsxs:element};
    if(name==='next/link')return {default:'a',__esModule:true};
    if(name==='@/lib/api')return {api(){throw Error('Provider must not be called')}};
    if(name.endsWith('.css'))return {};
    throw Error(name);
  }});
  return file==='express-quiz.tsx'?exports.default():exports.BaselineCard({projectId:1});
}
function nodes(tree){const out=[];function visit(n){if(Array.isArray(n))n.forEach(visit);else if(n&&typeof n==='object'&&n.props){out.push(n);visit(n.props.children)}}visit(tree);return out;}
function text(n){if(Array.isArray(n))return n.map(text).join('');if(n&&typeof n==='object')return text(n.props?.children);return n==null||typeof n==='boolean'?'':String(n);}
test('exhausted demo cap renders the result and manager message without credentials',()=>{
 const tree=render('express-quiz.tsx',{0:{questions:[]},1:{name:'Тест'},7:{calculation:{limit_cpl:300,current_cpl:400,recommendations:[['Проверка','Совет']]},demo:null,expires_at:null}});
 assert.match(text(tree),/Демо-кабинет пришлёт менеджер/);
 assert.doesNotMatch(text(tree),/Пароль:|Открыть демо-кабинет/);
 const fill=nodes(tree).find(n=>n.props.className==='fill'&&n.type==='i'&&n.props.style.background);
 assert.ok(fill);assert.equal(fill.props.style.background,'var(--warn)');
});
test('only agency admins see the baseline fixation button',()=>{
 for(const admin of [false,true]){
  const tree=render('brief-baseline.tsx',{0:{values:{fixed:false},history:[],can_edit:true,can_fix:admin},1:{fixed:false}});
  const buttons=nodes(tree).filter(n=>n.type==='button').map(n=>text(n));
  assert.ok(buttons.includes('Сохранить'));assert.equal(buttons.includes('Зафиксировать'),admin);
 }
});
test('all express quiz CSS selectors are isolated from the portal',()=>{
 const css=fs.readFileSync(path.join(__dirname,'../components/express-quiz.css'),'utf8');
 postcss.parse(css).walkRules(rule=>{
  if(rule.parent.type==='atrule'&&rule.parent.name==='keyframes')return;
  for(const selector of rule.selectors)assert.ok(selector.startsWith('.expressPage'),selector);
 });
 assert.match(css,/#006BFD/);assert.match(css,/#167BFF/);assert.match(css,/#F4F8FF/);assert.match(css,/border-radius:32px/);
});
