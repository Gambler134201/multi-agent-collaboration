'use strict';
// Tool-argument containment for Pi/DSH project mode. Not an OS sandbox:
// computed targets inside arbitrary scripts cannot be inferred here.
const path=require('path');
const policy=require('./record-policy.cjs');
function inside(root,value){
  const r=path.relative(policy.realpathDeep(root),policy.realpathDeep(value));
  return r!=='..'&&!r.startsWith('..'+path.sep)&&!path.isAbsolute(r);
}
function createGuard(config){
  const mode=config.access_mode||'full';
  if(!['full','project'].includes(mode))throw Error('执行端访问权限无效');
  const records=policy.createGuard(config);
  let stopped=false;
  return (name,args={})=>{
    const reason=records(name,args);if(reason)return reason;
    if(name===policy.PROBE_TOOL||mode==='full')return undefined;
    if(stopped)return '项目范围校验已拒绝此前调用；请报告主脑，不要换工具重试';
    const base=path.resolve(config.root,args.cwd||args.workdir||'.');
    const targets=[base];
    for(const key of ['path','file_path','filePath','notebook_path','target','directory']){
      if(typeof args[key]==='string')targets.push(path.resolve(base,args[key]));
    }
    if((policy.TERMINAL_TOOLS.has(name)||name==='powershell')&&typeof args.command==='string'){
      // Inspect literal filesystem targets; URLs and shell switches aren't paths.
      const tokens=args.command.match(/"[^"]*"|'[^']*'|[^\s;|&<>]+/g)||[];
      for(let token of tokens){
        token=token.replace(/^["']|["']$/g,'');
        if(/^[a-z]+:\/\//i.test(token))continue;
        if(path.isAbsolute(token)||/^(?:\.\.[\\/]|~[\\/])/.test(token)){
          if(/^~/.test(token)){stopped=true;return '项目范围不允许用户目录路径';}
          targets.push(path.resolve(base,token));
        }
      }
    }
    if(targets.some(p=>!inside(config.root,p))){
      stopped=true;return '目标位于本任务项目外；项目范围仅允许项目内普通文件，请报告主脑';
    }
    return undefined;
  };
}
module.exports={createGuard,inside};
