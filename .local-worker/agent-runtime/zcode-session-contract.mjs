// ZCode 3.16.3 session receipts. No tool implementation or permission grant.
export const READ_TOOLS=Object.freeze(['Read','Glob','Grep','WebFetch','WebSearch']);
export const EDIT_TOOLS=Object.freeze([...READ_TOOLS,'Write','Edit','Bash']);
const known=new Set(['build','yolo','plan','default']);
const object=v=>v!==null&&typeof v==='object'&&!Array.isArray(v);
const safe=v=>v===undefined?'<missing>':typeof v==='string'&&known.has(v)?v:'<unsupported>';

export function requestedMode(kind,native=true){
  if(!['chat','edit'].includes(kind))throw Error('ZCode task kind is invalid');
  // Read-only authority comes from the explicit allowlist AND native hook.
  // This host reports build, not plan, for its ordinary read-only sessions.
  return native&&kind==='chat'?'build':'yolo';
}
export function assertTools(kind,names){
  const expected=kind==='chat'?READ_TOOLS:kind==='edit'?EDIT_TOOLS:null;
  if(!expected||!Array.isArray(names)||names.length!==expected.length||new Set(names).size!==names.length||expected.some(n=>!names.includes(n)))
    throw Error('ZCode native tool allowlist is invalid');
}
export function assertReceipt(value,{stage,mode,sessionId,selection,transition=false}){
  const current=object(value)&&object(value.settings)&&object(value.settings.mode)?value.settings.mode.current:undefined;
  // createSession may return the initial build mode before explicit setMode.
  // Only the final setMode/readSession confirmation must match the request.
  if(!known.has(current)||(!transition&&current!==mode))
    throw Error(`ZCode ${stage}: mode not confirmed; requested=${safe(mode)} observed=${safe(current)}`);
  if(sessionId!==undefined&&(!object(value.session)||value.session.sessionId!==sessionId))
    throw Error(`ZCode ${stage}: session identity not confirmed`);
  if(selection){
    const actual=value.settings?.model?.current;
    if(!object(actual)||actual.providerId!==selection.providerId||actual.modelId!==selection.modelId)
      throw Error(`ZCode ${stage}: model identity not confirmed`);
  }
  return current;
}
