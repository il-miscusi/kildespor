export const areas = ['identity','accounts','people','web','hiring'] as const;
export const states = ['available','not_available','ambiguous','blocked','failed','not_applicable'] as const;
export type Area = typeof areas[number];
export type RecordItem = { [key: string]: unknown; sources?: string[] };
export type Coverage = { state: string; reason: string };
export type Company = { org: string; name: string; brand?: string; legalForm?: string; municipality?: string; industry?: string; employees?: number | null; website?: string; revenue?: number | null; coverage: Record<Area,Coverage>; brief: RecordItem[]; signals: RecordItem[]; facts: RecordItem[]; accounts: RecordItem[]; roles: RecordItem[]; workplaces: RecordItem[]; profiles: RecordItem[]; jobs: RecordItem[]; activity: RecordItem[]; changes: RecordItem[]; unknowns: RecordItem[]; evidence: RecordItem[] };
export type Dataset = { run: {runId: string; completedAt: string; count: number}; companies: Company[] };
const arrays = ['brief','signals','facts','accounts','roles','workplaces','profiles','jobs','activity','changes','unknowns','evidence'] as const;
export function parseDataset(input: unknown): Dataset {
 if (!input || typeof input !== 'object') throw Error('The data file is not a JSON object.');
 const d = input as Dataset;
 if (!d.run || typeof d.run.runId !== 'string' || !Array.isArray(d.companies)) throw Error('The data file needs run metadata and a companies list.');
 const seen = new Set<string>();
 return {...d, companies:d.companies.map(raw=>{
 if (!raw || raw.org == null || typeof raw.name !== 'string') throw Error('A company is missing its organisation number or name.');
 const c = {...raw, org:String(raw.org)};
 if(seen.has(c.org)) throw Error('Duplicate organisation number: '+c.org); seen.add(c.org);
 c.coverage = {...c.coverage};
 for (const area of areas) { const status=c.coverage[area]; if(status && !states.includes(status.state as typeof states[number])) throw Error('Unknown coverage state for '+c.name); c.coverage[area]=status || {state:'not_available',reason:'No reason supplied'}; }
 for(const key of arrays) { if(c[key] != null && !Array.isArray(c[key])) throw Error('Invalid '+key+' list for '+c.name); c[key]=(c[key]||[]).filter(x=>x&&typeof x==='object'); }
 return c;
 })};
}
export const text = (v: unknown): string => v == null || v === '' ? 'Not supplied' : typeof v === 'object' ? JSON.stringify(v) : String(v);
export const label = (v: unknown) => text(v).replaceAll('_',' ');
export const safeUrl = (v: unknown): string | undefined => { try {const u=new URL(String(v));return ['http:','https:'].includes(u.protocol)?u.href:undefined;}catch{return undefined;} };
export const numeric = (v:unknown): number | null => typeof v==='number'&&Number.isFinite(v)?v:null;
export const formatNumber=(v:unknown)=>numeric(v)===null?'Not supplied':new Intl.NumberFormat('en-GB',{maximumFractionDigits:2}).format(v as number);
export const dateText=(v:unknown)=> {const d=new Date(String(v));return Number.isNaN(d.valueOf())?'Not supplied':d.toLocaleDateString('en-GB',{day:'numeric',month:'short',year:'numeric',timeZone:'UTC'});};
export function activeJobs(c:Company, now = new Date()) {return c.jobs.filter(j=>(!j.published || new Date(String(j.published)).valueOf()<=now.valueOf())&&(!j.expires||new Date(String(j.expires)).valueOf()+86400000>now.valueOf()));}
const norm=(v:unknown)=>String(v??'').toLowerCase().replace(/[\s_-]/g,'');
export const aliases:Record<string,string[]>={legalForm:['legalForm','organisasjonsform'],municipality:['municipality','kommune'],industry:['industry','naering'],employees:['employees','ansatte'],revenue:['revenue','sales','driftsinntekter'],operating_result:['operating_result','operatingProfit','driftsresultat'],annual_result:['annual_result','net_income','aarsresultat'],equity_ratio:['equity_ratio','equityRatio','egenkapitalandel'],general_manager:['general_manager','daglig leder','CEO'],chair:['chair','chairperson','styrets leder']};
export function resolve(c:Company, field:string): {value:unknown;sources:string[];period?:unknown;currency?:unknown;area:Area} {
 const keys=(aliases[field]||[field]).map(norm); const f=c.facts.find(f=>keys.includes(norm(f.field)));
 const area:Area=['revenue','operating_result','annual_result','equity_ratio'].includes(field)?'accounts':['general_manager','chair','employees'].includes(field)?'people':field==='website'?'web':'identity';
 if(['general_manager','chair'].includes(field)){const r=c.roles.find(r=>keys.includes(norm(r.role)));if(r)return {value:r.name,sources:r.sources||[],period:r.since,area};}
 const acc=c.accounts.filter(a=>keys.includes(norm(a.metric))).sort((a,b)=>Number(b.year)-Number(a.year))[0];
 const top=c[field as keyof Company];
 if(acc&&top==null)return {value:acc.amount,sources:acc.sources||[],period:acc.year,currency:acc.currency,area};
 if(top!=null&&typeof top!=='object')return {value:top,sources:f&&String(f.value)===String(top)?f.sources||[]:acc&&acc.amount===top?acc.sources||[]:[],period:f?.period??f?.date??(acc?.amount===top?acc.year:undefined),currency:acc?.amount===top?acc.currency:undefined,area};
 if(f)return {value:f.value,sources:f.sources||[],period:f.period??f.date,area};
 if(acc)return {value:acc.amount,sources:acc.sources||[],period:acc.year,currency:acc.currency,area};
 return {value:null,sources:[],area};
}
export function dataScore(c:Company){return c.facts.filter(f=>f.value!=null&&f.sources?.length).length+c.accounts.filter(a=>a.amount!=null&&a.sources?.length).length+['brief','roles','workplaces','profiles','jobs','activity'].reduce((n,k)=>n+(c[k as keyof Company] as RecordItem[]).filter(x=>x.sources?.length).length,0);}
export function filterCompanies(companies:Company[], query:string, filters:string[], sort:string, now=new Date()) {
 const q=query.toLowerCase().trim();
 return companies.filter(c=>[c.name,c.brand,c.org,c.industry,c.municipality].some(v=>String(v??'').toLowerCase().includes(q))||Boolean(q)&&c.org.toLowerCase().replaceAll(' ','').includes(q.replaceAll(' ',''))).filter(c=>filters.every(f=>f==='website'?!!safeUrl(c.website):f==='hiring'?activeJobs(c,now).length>0:f==='accounts'?c.accounts.some(a=>a.amount!=null):areas.every(a=>c.coverage[a].state==='available'))).sort((a,b)=>{
 const tie=a.name.localeCompare(b.name,'nb')||a.org.localeCompare(b.org);
 if(sort==='name')return tie;
 if(sort==='data')return dataScore(b)-dataScore(a)||areas.filter(x=>b.coverage[x].state==='available').length-areas.filter(x=>a.coverage[x].state==='available').length||tie;
 const av=numeric(resolve(a,sort).value),bv=numeric(resolve(b,sort).value);return av===null?(bv===null?tie:1):bv===null?-1:bv-av||tie;
 });
}
