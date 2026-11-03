'use strict';
// Tool-argument containment for Pi/DSH project mode. Not an OS sandbox:
// computed targets inside arbitrary scripts cannot be inferred here.
const path=require('path');
const policy=require('./record-policy.cjs');
function inside(root,value){
  const r=path.relative(policy.realpathDeep(root),policy.realpathDeep(value));
  return r!=='..'&&!r.startsWith('..'+path.sep)&&!path.isAbsolute(r);
}
function directPaths(config,args,base){
  return policy.directTerminalTargets(args.command,base).filter(row=>!row.gitMutation).map(row=>{
    const value=/^~(?:[\\/]|$)/.test(row.value)?config.task_policy.home+row.value.slice(1):row.value;
    return policy.resolveAgainst(row.base,value);
  });
}
function isolationReason(config,name,args={}){
  if(!config.record_project||path.resolve(config.record_project)===path.resolve(config.root))return undefined;
  const base=path.resolve(config.root,args.cwd||args.workdir||'.'),targets=[base];
  for(const key of ['path','file_path','filePath','notebook_path','target','directory']){
    if(typeof args[key]==='string')targets.push(path.resolve(base,args[key]));
  }
  if((policy.TERMINAL_TOOLS.has(name)||['Bash','PowerShell','powershell'].includes(name))&&typeof args.command==='string'){
    if(policy.directTerminalPolicy(config))targets.push(...directPaths(config,args,base));
    else {
    args={...args,command:policy.terminalAnalysis(args.command)};
    const tokens=args.command.match(/"[^"]*"|'[^']*'|[^\s;|&<>]+/g)||[];
    for(let token of tokens){
      token=token.replace(/^["']|["']$/g,'');
      if(/^[a-z]+:\/\//i.test(token))continue;
      if(path.isAbsolute(token)||/^(?:\.\.[\\/])/.test(token))targets.push(path.resolve(base,token));
    }
    const facts=policy.terminalFacts(args.command,base);
    targets.push(...facts.locations);
    if(facts.overflow)return '复合命令过于复杂，无法核实工作区隔离；请报告主脑';
    }
  }
  for(const target of targets){
    for(const p of [path.resolve(target),policy.realpathDeep(target)]){
      const read=['read','Read','grep','Grep','find','Glob','ls','NotebookRead'].includes(name);
      if(read&&config.context_root&&inside(config.context_root,p))continue;
      if(inside(config.record_project,p)||(/(?:^|[\\/])[^\\/]+-task-workspaces(?:[\\/]|$)/i.test(p)&&!inside(config.root,p))){
        return '任务隔离不允许访问原项目或其他任务工作区；请报告主脑';
      }
    }
  }
  return undefined;
}
function createGuard(config){
  const mode=config.access_mode||'full';
  if(!['full','project'].includes(mode))throw Error('执行端访问权限无效');
  policy.taskPolicy(config);
  const records=policy.createGuard(config);
  let stopped=false;
  return (name,args={})=>{
    // v3 terminals have one direct-target decision below. The legacy record
    // wrapper appends text and scans separately; do not double-run it here.
    const direct=policy.directTerminalPolicy(config);
    const directShell=direct
      &&['bash','pwsh','powershell','Bash','PowerShell'].includes(name);
    const reason=directShell?undefined:records(name,args);
    if(reason){if(direct&&name!==policy.PROBE_TOOL)stopped=true;return reason;}
    if(direct&&stopped&&name!==policy.PROBE_TOOL)return '本轮工具已暂停：此前触发任务保护，请报告主脑，不要换工具重试';
    if(name!==policy.PROBE_TOOL){
      const isolated=isolationReason(config,name,args);
      if(isolated){stopped=true;return isolated;}
      if(stopped&&config.record_project)return '工作区隔离已拒绝此前调用；请报告主脑，不要换工具重试';
      if(stopped&&config.task_policy)return '任务文件范围已拒绝此前调用；请报告主脑，不要换工具重试';
      const protectedReason=policy.protectedArgumentsReason(config,name,args);
      if(protectedReason){stopped=true;return protectedReason;}
      if(config.task_policy?.write_scope==='read_only'&&['bash','pwsh','powershell'].includes(name)){
        stopped=true;return '只读任务没有终端权限';
      }
      if(['write','edit','str_replace_editor'].includes(name)
          &&!(name==='str_replace_editor'&&args.command==='view')){
        const value=args.file_path??args.path;
        const reason=policy.taskWriteReason(config,typeof value==='string'&&value.trim()
          ?path.resolve(config.root,args.workdir||args.cwd||'.',value):value);
        if(reason){stopped=true;return reason;}
      }
    }
    if(name===policy.PROBE_TOOL||mode==='full')return undefined;
    if(stopped)return '项目范围校验已拒绝此前调用；请报告主脑，不要换工具重试';
    const base=path.resolve(config.root,args.cwd||args.workdir||'.');
    const targets=[base];
    for(const key of ['path','file_path','filePath','notebook_path','target','directory']){
      if(typeof args[key]==='string')targets.push(path.resolve(base,args[key]));
    }
    if((policy.TERMINAL_TOOLS.has(name)||name==='powershell')&&typeof args.command==='string'){
      if(policy.directTerminalPolicy(config))targets.push(...directPaths(config,args,base));
      else {
      args={...args,command:policy.terminalAnalysis(args.command)};
      const facts=policy.terminalFacts(args.command,base);
      targets.push(...facts.locations);
      if(facts.overflow){stopped=true;return '复合命令过于复杂，无法核实项目范围；请报告主脑';}
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
    }
    const contextRead=['read','Read','grep','Grep','find','Glob','ls','NotebookRead'].includes(name);
    if(targets.some(p=>!inside(config.root,p)&&!(contextRead&&config.context_root&&inside(config.context_root,p)))){
      stopped=true;return '目标位于本任务项目外；项目范围仅允许项目内普通文件，请报告主脑';
    }
    return undefined;
  };
}
module.exports={createGuard,inside,isolationReason};
