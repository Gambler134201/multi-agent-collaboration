// Read shipped declarations from the installed DSH; never read desktop credentials.
const fs=require('fs');
const root=process.argv[2]+'/dsh/node_modules/@deepseek-ai/dsh-web-app/';
process.stdout.write(JSON.stringify({standard:fs.readFileSync(root+'presets/standard.patch.yml','utf8'),web:fs.readFileSync(root+'cordis.patch.yml','utf8')}));
