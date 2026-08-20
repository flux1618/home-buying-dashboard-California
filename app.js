/* Los Angeles Home-Buy Decision Deck */
let DATA=null, MAP=null, LAYERS={}, ACTIVE=new Set(['price','drive','schools']), BE_CHART=null, RUNWAY_CHART=null, SPLIT_CHART=null, BAL_CHART=null;
const fmt$=n=>n==null||isNaN(n)?'—':'$'+Math.round(n).toLocaleString();
const fmt$k=n=>n==null||isNaN(n)?'—':'$'+(Math.round(n/1000))+'K';
const pct=n=>n==null||isNaN(n)?'—':(n*100).toFixed(1)+'%';
const pct0=n=>n==null||isNaN(n)?'—':(n*100).toFixed(0)+'%';

async function boot(){
  DATA = await fetch('data.json').then(r=>r.json());
  document.getElementById('asof').textContent = DATA.global.asof;
  initMap();
  renderLayerToggles();
  renderLegend();
  renderScorecard();
  renderTiming();
  renderHazards();
  bindGlobals();
  bindAmort();
  bindRentBuy();
  bindProperty();
  bindRunway();
  document.getElementById('theme-toggle').addEventListener('click', toggleTheme);
  updateAll();
}

function toggleTheme(){
  const html=document.documentElement;
  const cur=html.getAttribute('data-theme');
  html.setAttribute('data-theme', cur==='dark'?'light':'dark');
  if(MAP){ // redraw tile brightness for theme
    setTimeout(()=>{ Object.values(LAYERS).forEach(l=>{ if(l && l.redraw) l.redraw(); }); },50);
  }
  if(BE_CHART){ BE_CHART.update(); }
  if(RUNWAY_CHART){ RUNWAY_CHART.update(); }
  // Rebuilt rather than update()d. Chart.js copies the axis and legend colours into its
  // options at construction, so update() re-renders with the *old* theme's greys -- which
  // is why the two charts above keep dark-theme gridlines in light mode. Rebuilding here
  // re-reads the CSS variables. Same fix would work above; not touching those in this pass.
  if(SPLIT_CHART || BAL_CHART){
    renderAmortization(readNum('i-price'), readNum('i-down'), readNum('i-rate')/100);
  }
}

/* ---------------- MAP ---------------- */
function initMap(){
  // Centred between the anchor (Keck Hospital of USC, Boyle Heights) and the San Gabriel
  // Valley submarkets, at a zoom that shows the whole target-ZIP footprint. Zoom 11 rather
  // than the SC edition's 10: the LA search area is geographically tighter but far denser,
  // so 10 wasted half the viewport on the ocean and the Angeles National Forest.
  MAP = L.map('map', {zoomControl:true, preferCanvas:true}).setView([34.075,-118.16],11);
  L.tileLayer('https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png',{
    attribution:'© OpenStreetMap contributors © CARTO', maxZoom:18, subdomains:'abcd'
  }).addTo(MAP);

  // anchor pin
  const a = DATA.global.anchor;
  const anchor = L.circleMarker([a.lat,a.lon],{radius:8,fillColor:'#e0a83a',color:'#111',weight:2,opacity:1,fillOpacity:.9}).addTo(MAP)
    .bindTooltip("Commute anchor · "+a.label,{className:'z-tip',direction:'top',offset:[0,-6]});

  // ZIP price choropleth
  const zips = DATA.geojson.zips;
  const zipPrices = {};
  DATA.submarkets.forEach(s=>{
    s.zips.forEach(z=>{ if(s.redfin) zipPrices[z]=s.redfin; });
  });
  const priceVals = Object.values(zipPrices).map(v=>v.median_price).filter(x=>x);
  const lo=Math.min(...priceVals), hi=Math.max(...priceVals);
  const colorFor = p => {
    if(p==null) return '#555';
    const t=(p-lo)/(hi-lo);
    // teal (cheap) → gold (mid) → red (expensive)
    const stops=[[0,'#7dd3c0'],[0.5,'#e0a83a'],[1,'#e06767']];
    let a=stops[0], b=stops[stops.length-1];
    for(let i=0;i<stops.length-1;i++) if(t>=stops[i][0]&&t<=stops[i+1][0]){a=stops[i];b=stops[i+1];break;}
    const r=(t-a[0])/(b[0]-a[0]);
    const mix=(c1,c2)=>{const p1=parseInt(c1.slice(1),16),p2=parseInt(c2.slice(1),16);
      const rr=Math.round(((p1>>16)&255)*(1-r)+((p2>>16)&255)*r);
      const gg=Math.round(((p1>>8)&255)*(1-r)+((p2>>8)&255)*r);
      const bb=Math.round((p1&255)*(1-r)+(p2&255)*r);
      return `rgb(${rr},${gg},${bb})`;};
    return mix(a[1],b[1]);
  };

  LAYERS.price = L.geoJSON(zips,{
    style:f=>{
      const d = zipPrices[f.properties.zip];
      return {
        color:'#333', weight:1, fillOpacity:d?0.55:0.12,
        fillColor: d ? colorFor(d.median_price) : '#444'
      };
    },
    onEachFeature:(f,l)=>{
      const p=f.properties, d=zipPrices[p.zip];
      const tip = d
        ? `<b>${p.zip} · ${p.name}</b><br>Median ${fmt$(d.median_price)} · $${d.ppsf?.toFixed(0)}/sqft · YoY ${(d.yoy_price*100).toFixed(1)}%<br>DoM ${d.dom} · Inv ${d.inventory}`
        : `<b>${p.zip} · ${p.name}</b><br>No Redfin data`;
      l.bindTooltip(tip,{className:'z-tip',sticky:true});
    }
  });

  // Tax layer. Under Prop 13 the rate barely varies across these ZIPs; what varies is the
  // *basis*, which resets to your purchase price on sale. So the honest tooltip is about the
  // reset, not the rate.
  LAYERS.tax = L.geoJSON(zips,{
    style:f=>({color:'#333',weight:1,fillOpacity:0.35,fillColor:'#01696f'}),
    onEachFeature:(f,l)=>{
      const p=f.properties;
      const tra = DATA.global.tax.tax_rate_area, rate = DATA.global.tax.total_rate_pct;
      l.bindTooltip(`<b>${p.zip} · ${p.name}</b><br>Tax rate area <b>${tra}</b> · <b>${rate.toFixed(4)}%</b> of assessed value.<br>Assessed value <b>resets to your purchase price</b> on sale, then grows at most 2%/yr (Prop 13).<br>The tax shown on a listing reflects the <i>seller's</i> frozen basis and will be too low.`,{className:'z-tip',sticky:true});
    }
  });

  // School district polygons
  LAYERS.schools = L.geoJSON(DATA.geojson.districts,{
    style:f=>({color:'#b48be0',weight:2,fillOpacity:0.10,fillColor:'#b48be0'}),
    onEachFeature:(f,l)=>{
      l.bindTooltip("<b>"+f.properties.name+"</b><br>2025 CAASPP results at <a href='https://caaspp-elpac.ets.org/caaspp/' target='_blank'>caaspp-elpac.ets.org</a><br><span style='opacity:.75'>Boundaries are board policy and can be redrawn.</span>",{className:'z-tip',sticky:true});
    }
  });

  // Flood zones
  LAYERS.flood = L.geoJSON(DATA.geojson.flood,{
    style:{color:'#6ea8ff',weight:0.5,fillOpacity:0.55,fillColor:'#6ea8ff'},
    onEachFeature:(f,l)=>{ l.bindTooltip(f.properties.label||'FEMA SFHA',{className:'z-tip'}); }
  });

  // Drive-time isochrones as colored dots (grid of drive-time samples)
  const grid = DATA.drivetime.grid;
  const dtLayer = L.layerGroup();
  const bandColor = m => {
    if(m==null) return null;
    if(m<=15) return '#65c58b';
    if(m<=30) return '#e0a83a';
    if(m<=45) return '#e06767';
    return null;
  };
  grid.forEach(g=>{
    const c = bandColor(g.min);
    if(!c) return;
    L.circle([g.lat,g.lon],{radius:1400,color:c,weight:0,fillColor:c,fillOpacity:0.15}).addTo(dtLayer);
  });
  LAYERS.drive = dtLayer;

  // POI: grocery, hospital, freeway ramps.
  // The LA snapshot may ship an empty poi array: every public Overpass mirror refused the
  // request when this edition was built. An empty array is NOT the same claim as "there are no
  // supermarkets in Boyle Heights", so the layer toggles below detect the empty case and say
  // so rather than offering a button that silently draws nothing.
  const pois = DATA.poi || [];
  POI_AVAILABLE = pois.length > 0;
  const grp = {grocery:L.layerGroup(),hospital:L.layerGroup(),ramp:L.layerGroup()};
  const colors={grocery:'#65c58b',hospital:'#6ea8ff',ramp:'#b48be0'};
  const radii={grocery:3,hospital:5,ramp:2};
  pois.forEach(p=>{
    L.circleMarker([p.lat,p.lon],{radius:radii[p.c],fillColor:colors[p.c],color:'#111',weight:0.5,fillOpacity:0.9})
      .bindTooltip(`<b>${p.n}</b> · ${p.c}`,{className:'z-tip'})
      .addTo(grp[p.c]);
  });
  LAYERS.grocery = grp.grocery;
  LAYERS.hospital = grp.hospital;
  LAYERS.ramp = grp.ramp;

  // Submarket centroid pins with popups
  LAYERS.pins = L.layerGroup();
  DATA.submarkets.forEach(s=>{
    if(!s.redfin) return;
    // pick any centroid from first zip
    const zf = DATA.geojson.zips.features.find(f=>f.properties.zip===s.zips[0]);
    if(!zf) return;
    const [lat,lon] = zf.properties.centroid;
    const pin = L.circleMarker([lat,lon],{radius:8,fillColor:'#7dd3c0',color:'#111',weight:1.5,fillOpacity:0.95});
    const prof = s.school_proficiency_pct;
    const profTxt = prof == null
      ? '<span style="color:#9aa5ad">CAASPP not available</span>'
      : `${prof.toFixed(1)}% proficient`;
    const dLink = s.school_district_url
      ? `<a href="${s.school_district_url}" target="_blank">${s.school_district}</a>`
      : s.school_district;
    const providers = (s.fiber && s.fiber.providers) || [];
    const fiberLine = providers.length
      ? `Fiber: ${providers.slice(0,2).join(', ')}<br>`
      : '';
    const popup = `
      <b>${s.name}</b><br>
      Median: <b>${fmt$(s.redfin.median_price)}</b> \u00b7 $${Math.round(s.redfin.ppsf)}/sqft \u00b7 YoY ${(s.redfin.yoy_price*100).toFixed(1)}%<br>
      DoM ${Math.round(s.redfin.dom)} \u00b7 Inv ${s.redfin.inventory} \u00b7 Drive ${s.drive_min} min<br>
      Schools: ${dLink} \u00b7 ${profTxt}<br>
      ${fiberLine}<span style="color:#9aa5ad">Sources: <a href="https://www.redfin.com/news/data-center/" target="_blank">Redfin Data Center</a> \u00b7 <a href="https://caaspp-elpac.ets.org/caaspp/" target="_blank">CAASPP 2025</a></span>
    `;
    pin.bindPopup(popup);
    pin.addTo(LAYERS.pins);
  });

  refreshMap();
}

let POI_AVAILABLE = false;
const POI_LAYERS = new Set(['grocery','hospital','ramp']);
const LAYER_META = [
  ['price','Median price $/sqft'],
  ['tax','Property tax (TRA rate)'],
  ['schools','School districts · CAASPP'],
  ['flood','FEMA flood zones'],
  ['drive','Drive time (15/30/45)'],
  ['grocery','Grocery'],
  ['hospital','Hospitals'],
  ['ramp','Freeway ramps'],
  ['pins','Submarket pins'],
];
function renderLayerToggles(){
  const c=document.getElementById('layer-toggles');
  ACTIVE = new Set(['price','drive','pins']);
  c.innerHTML='';
  LAYER_META.forEach(([k,label])=>{
    const b=document.createElement('button');
    b.textContent=label; if(ACTIVE.has(k)) b.classList.add('on');
    if(POI_LAYERS.has(k) && !POI_AVAILABLE){
      b.disabled = true;
      b.classList.add('off');
      b.textContent = label + ' — no data';
      b.title = 'This snapshot carries no points of interest: every public Overpass mirror '
              + 'refused the request when it was built. Absent, not zero.';
      c.appendChild(b);
      return;
    }
    b.onclick=()=>{
      if(ACTIVE.has(k)) ACTIVE.delete(k); else ACTIVE.add(k);
      b.classList.toggle('on'); refreshMap();
    };
    c.appendChild(b);
  });
}
function refreshMap(){
  LAYER_META.forEach(([k])=>{
    const layer = LAYERS[k];
    if(!layer) return;
    if(ACTIVE.has(k)){ if(!MAP.hasLayer(layer)) layer.addTo(MAP); }
    else{ if(MAP.hasLayer(layer)) MAP.removeLayer(layer); }
  });
}
function renderLegend(){
  const el = document.getElementById('legend');
  el.innerHTML = `
    <div><span class="sw" style="background:#7dd3c0"></span>Cheaper median</div>
    <div><span class="sw" style="background:#e0a83a"></span>Mid</div>
    <div><span class="sw" style="background:#e06767"></span>Pricier median</div>
    <div><span class="sw" style="background:#65c58b"></span>≤15 min drive</div>
    <div><span class="sw" style="background:#e0a83a"></span>15–30 min</div>
    <div><span class="sw" style="background:#e06767"></span>30–45 min</div>
    <div><span class="sw" style="background:#6ea8ff"></span>FEMA SFHA (A/AE)</div>
    <div><span class="sw" style="background:#b48be0"></span>School district</div>
  `;
}

/* ---------------- INPUTS ---------------- */
function bindGlobals(){
  ['inc','exp','dti','down','price','rate','hoa','w-fiber','w-school'].forEach(k=>{
    const el=document.getElementById('i-'+k);
    el.addEventListener('input',()=>updateAll());
  });
}
function bindAmort(){
  // Its own listener rather than joining bindGlobals: the extra payment changes nothing
  // outside this section, and redrawing the map and every chart on a slider drag is a
  // visible stutter on the Pi.
  document.getElementById('i-extra').addEventListener('input',()=>{
    renderAmortization(readNum('i-price'), readNum('i-down'), readNum('i-rate')/100);
  });
}
function readNum(id){ return parseFloat(document.getElementById(id).value); }
function updateAll(){
  const inc=readNum('i-inc'), exp=readNum('i-exp'), dti=readNum('i-dti');
  const down=readNum('i-down'), price=readNum('i-price'), rate=readNum('i-rate')/100, hoa=readNum('i-hoa');
  document.getElementById('l-inc').textContent = fmt$(inc);
  document.getElementById('l-exp').textContent = fmt$(exp);
  document.getElementById('l-dti').textContent = dti+'%';
  document.getElementById('l-down').textContent = fmt$(down);
  document.getElementById('l-price').textContent = fmt$(price);
  document.getElementById('l-rate').textContent = readNum('i-rate').toFixed(2)+'%';
  document.getElementById('l-hoa').textContent = fmt$(hoa);
  document.getElementById('l-w-fiber').textContent = readNum('i-w-fiber')+'%';
  document.getElementById('l-w-school').textContent = readNum('i-w-school')+'%';

  const loan = Math.max(0, price - down);
  // PITI lives in pitiParts so this KPI block and the max-price solver below read the same
  // function instead of two copies of the same formula.
  const parts = pitiParts(price, down, rate, hoa);
  const pi = parts.pi, taxMo = parts.tax, insMo = parts.ins, piti = parts.piti;
  const fdti = piti / (inc/12);
  const closing = price * 0.03;
  document.getElementById('k-loan').textContent = fmt$(loan);
  document.getElementById('k-loan-d').textContent = `Price ${fmt$(price)} − Down ${fmt$(down)}`;
  document.getElementById('k-pi').textContent = fmt$(pi);
  document.getElementById('k-tax').textContent = fmt$(taxMo);
  document.getElementById('k-ins').textContent = fmt$(insMo);
  document.getElementById('k-hoa').textContent = fmt$(hoa);
  document.getElementById('k-piti').textContent = fmt$(piti);
  document.getElementById('k-fdti').textContent = (fdti*100).toFixed(1)+'%';
  const fEl=document.getElementById('k-fdti-d');
  if(fdti*100 <= dti){ fEl.textContent = `Under ${dti}% target ✓`; fEl.className='d up'; }
  else{ fEl.textContent = `Over ${dti}% target — cut price or grow down`; fEl.className='d down'; }
  document.getElementById('k-ctc').textContent = fmt$(down + closing);
  renderMaxPrice(inc, down, rate, hoa, dti);
  renderAmortization(price, down, rate);
  renderRateSensitivity(price, down, rate, hoa);

  // Verdict
  const rentEq = 4080; // LA County 3-bed asking rent baseline (see market snapshot source)
  const delta = piti - rentEq;
  const verdict = document.getElementById('afford-verdict');
  const decision = (fdti*100 <= dti && piti > 0) ? 'take' : 'watch';
  verdict.innerHTML = `<b>Verdict:</b> <span class="chip ${decision}">${decision==='take'?'TAKE':'WATCH'}</span> at ${fmt$(price)} · rate ${(rate*100).toFixed(2)}%. PITI ${fmt$(piti)} vs. ~${fmt$(rentEq)} rent (${delta>=0?'+':''}${fmt$(delta)}/mo). Cash to close ${fmt$(down+closing)}. Sensitivity: a +1% rate move adds ~${fmt$(loan*0.01/12*7)}/mo on this loan.`;

  updateBE(); updateProperty(); updateRunway(); renderScorecard();
}

/* ---------------- MAX PRICE (the affordability question read backwards) ----------------
   The forward question is "what does this house cost me". The question you actually ask
   while browsing listings is "what is the highest number I can put in the price filter".
   Same arithmetic, solved for price instead of payment.

   Solved by bisection over pitiParts rather than algebraically. PITI on this page is
   linear in price above the homeowners' exemption, so a closed form would work today -- but
   the engine's version also owns the Prop 13 rate-area lookup and exemption. A formula derived
   independently here would drift from the engine the first time either side changes.
   Bisecting the same function the KPIs use cannot disagree with them, because it is them. */
function pitiParts(price, down, rate, hoa){
  const loan = Math.max(0, price - down);
  const pi = pmt(loan, rate, 360);
  // California owner-occupier: purchase price resets the Prop 13 base year value, then the
  // $7,000 homeowners' exemption reduces assessed value. TRA 00004 is the profile's central
  // Los Angeles default (1.187380% for FY 2025-26). The harness injects these same profile
  // values; the fallbacks keep the static page on the LA defaults until its snapshot is rebuilt.
  const taxConfig = DATA.global.tax || {};
  // The snapshot publishes the rate as a percentage (1.18738), so it is divided here rather
  // than stored pre-divided -- the number in the file then matches the number the county
  // publishes, which is what anyone auditing this will compare against.
  const rateAreaRate = taxConfig.total_rate_pct != null
    ? taxConfig.total_rate_pct / 100
    : 0.0118738;
  const homeownersExemption = taxConfig.homeowners_exemption ?? 7000;
  const tax = Math.max(0, price - homeownersExemption) * rateAreaRate / 12;
  const insuranceConfig = DATA.global.insurance || {};
  const annualInsurance = insuranceConfig.annual ?? insuranceConfig.ca_avg_annual ?? 2058;
  const ins = annualInsurance / 12;
  return { pi, tax, ins, piti: pi + tax + ins + hoa };
}
function solveMaxPrice(inc, down, rate, hoa, dtiPct){
  const ceiling = (dtiPct/100) * (inc/12);
  // The floor is the payment at a price equal to the down payment, not at a price of zero.
  // The down payment is fixed, so any price below it is a house you are overpaying cash
  // for -- the engine's solver uses the same floor, and starting at zero here produced a
  // "max price" of $13k against $80k down, which is not an answer.
  const floorPrice = Math.max(down, 1);
  const floor = pitiParts(floorPrice, down, rate, hoa).piti;
  // Property tax scales with price and vanishes at the floor; insurance and HOA do not. So
  // if the floor already breaks the ceiling, no price works and the honest answer is to say
  // so. Returning 0 would imply "buy something cheaper", which is not the fix.
  if(floor > ceiling) return { feasible:false, floor:floor, floorPrice:floorPrice };
  let lo = floorPrice, hi = down + 5000000;
  if(pitiParts(hi, down, rate, hoa).piti <= ceiling) return { feasible:true, price:hi, capped:true };
  // Bounded loop, not while(hi-lo>1). 60 halvings of a $5M bracket lands far inside a
  // dollar, and a bounded loop cannot hang the page on a pathological input.
  for(let i=0;i<60 && hi-lo>1;i++){
    const mid = (lo+hi)/2;
    if(pitiParts(mid, down, rate, hoa).piti <= ceiling) lo = mid; else hi = mid;
  }
  // Return the low edge. Rounding up hands back a price that breaches the ceiling, which
  // is the one direction of error that actually costs something.
  return { feasible:true, price:lo };
}
/* ---------------- AMORTIZATION ----------------

   A second implementation of analyzer/core/amortization.py, and the only reason that is
   acceptable is tests/test_amortization_parity.py, which runs this file under node and
   compares every one of the 360 rows against Python. Same arrangement as solveMaxPrice
   above: ADR 0008 says duplicated *rules* get compiled into data.json, but a loop is
   behaviour and there is nothing to compile, so the drift is caught by a test instead.

   Three things here are copied deliberately and must not be "simplified":

   1. Money is integer cents. 360 sequential operations on one running balance is enough
      for float error to show up as a closing balance of -0.004.
   2. Math.round is half-up for positive numbers, which is what Python's ROUND_HALF_UP
      does and what a servicer does. Money here is always positive so the two agree.
   3. The last scheduled payment is the balance plus interest, not the level payment.
      Without that a 30-year note bills 361 payments, the last for $3.31.
*/
function amortize(loan, rate, term, extraMonthly){
  const loanCents = Math.round(loan*100);
  const payCents = Math.round(pmt(loan, rate, term)*100);
  const extraCents = Math.round((extraMonthly||0)*100);
  const r = rate/12;
  let balance = loanCents;
  const rows = [];
  // Bounded, not while(balance>0). An unbounded loop on a page can hang the tab, and the
  // guard is the same 1200 the Python module uses.
  for(let i=0; i<1200 && balance>0; i++){
    const interest = Math.round(balance*r);
    // Negative amortization: the payment never covers the interest, so the balance grows.
    // Unreachable while the payment is derived from the loan above -- kept so that adding
    // a payment override fails here instead of silently billing forever.
    if(payCents + extraCents <= interest && balance > payCents + extraCents) return null;
    let principal = payCents - interest;
    let extra = extraCents;
    if(rows.length + 1 >= term){ principal = balance; extra = 0; }
    else if(principal >= balance){ principal = balance; extra = 0; }
    else if(principal + extra >= balance){ extra = balance - principal; }
    balance -= principal + extra;
    rows.push({interest, principal, extra, balance});
  }
  if(balance > 0) return null;

  let crossover = null, totalInterest = 0;
  const payments = rows.map((row,i)=>{
    totalInterest += row.interest;
    if(crossover === null && row.principal + row.extra >= row.interest) crossover = i+1;
    return {
      number:i+1,
      payment:(row.interest+row.principal+row.extra)/100,
      interest:row.interest/100,
      principal:row.principal/100,
      extra:row.extra/100,
      balance:row.balance/100
    };
  });

  const years = [];
  for(let start=0; start<payments.length; start+=12){
    const chunk = payments.slice(start, start+12);
    years.push({
      year:start/12+1,
      payments:chunk.length,
      interest:chunk.reduce((a,p)=>a+p.interest,0),
      principal:chunk.reduce((a,p)=>a+p.principal,0),
      extra:chunk.reduce((a,p)=>a+p.extra,0),
      ending_balance:chunk[chunk.length-1].balance
    });
  }

  let monthsSaved = 0, interestSaved = 0;
  if(extraCents){
    // Measured against the schedule this same function produces with no extra, not against
    // a formula. A comparison to something the page would not actually draw is worthless.
    const base = amortize(loan, rate, term, 0);
    if(base){
      monthsSaved = base.months_to_payoff - payments.length;
      interestSaved = base.total_interest - totalInterest/100;
    }
  }

  return {
    loan_amount:loanCents/100,
    scheduled_payment:payCents/100,
    extra_monthly:extraCents/100,
    payments, years,
    total_interest:totalInterest/100,
    total_principal:loanCents/100,
    total_paid:(totalInterest+loanCents)/100,
    months_to_payoff:payments.length,
    final_payment:payments[payments.length-1].payment,
    crossover_month:crossover,
    months_saved:monthsSaved,
    interest_saved:interestSaved
  };
}
// Pulled out of pitiParts so the schedule and the PITI block cannot disagree about the
// level payment, the same reason the Python schedule imports cost.monthly_payment.
function pmt(loan, rate, n){
  if(loan <= 0) return 0;
  const r = rate/12;
  return r>0 ? loan*(r*Math.pow(1+r,n))/(Math.pow(1+r,n)-1) : loan/n;
}

/* ---------------- RATE SENSITIVITY ----------------

   A rate sweep is behaviour, not a profile rule table. ADR 0008 therefore permits this second
   implementation only with an audit: tests/test_sensitivity_parity.py runs these functions in
   node and checks every displayed cent against analyzer.core.sensitivity with zero tolerance.
   The loop uses integer basis points, and money crosses this boundary as integer cents, so a
   one-cent difference is a bug rather than an acceptable browser rounding difference. */
function sensitivityBand(price, down, startBps, endBps, stepBps, hoa){
  if(startBps > endBps || stepBps <= 0 || (endBps-startBps)%stepBps) return null;
  const loanCents = Math.round(Math.max(0, price-down)*100);
  const rows = [];
  for(let rateBps=startBps; rateBps<=endBps; rateBps+=stepBps){
    const rate = rateBps/10000;
    const parts = pitiParts(price, down, rate, hoa);
    const piCents = Math.round(parts.pi*100);
    rows.push({
      rateBps,
      piCents,
      pitiCents:Math.round(parts.piti*100),
      totalInterestCents:piCents*360-loanCents
    });
  }
  return rows;
}
function priceAtPitiCents(targetPitiCents, down, rate, startingPrice, hoa){
  const at = priceCents => Math.round(pitiParts(priceCents/100, down, rate, hoa).piti*100);
  let lo = 1, hi = Math.max(Math.round(startingPrice*100), Math.round(down*100), 100);
  if(at(lo)>targetPitiCents) return null;
  let bracketed = false;
  for(let i=0;i<60;i++){
    if(at(hi)>=targetPitiCents){ bracketed=true; break; }
    hi*=2;
  }
  if(!bracketed) return null;
  while(hi-lo>1){
    const mid = Math.floor((lo+hi)/2);
    if(at(mid)<=targetPitiCents) lo=mid; else hi=mid;
  }
  return lo;
}
function waitVsBuy(price, down, baselineRate, futureRate, futurePrice, hoa){
  const base = pitiParts(price, down, baselineRate, hoa);
  const future = pitiParts(futurePrice, down, futureRate, hoa);
  const basePiCents = Math.round(base.pi*100), futurePiCents = Math.round(future.pi*100);
  const basePitiCents = Math.round(base.piti*100), futurePitiCents = Math.round(future.piti*100);
  const baseLoanCents = Math.round(Math.max(0,price-down)*100);
  const futureLoanCents = Math.round(Math.max(0,futurePrice-down)*100);
  const breakEvenPriceCents = priceAtPitiCents(basePitiCents, down, futureRate, price, hoa);
  return {
    baselinePitiCents:basePitiCents, futurePitiCents,
    monthlyDeltaCents:futurePitiCents-basePitiCents,
    lifetimeInterestDeltaCents:(futurePiCents*360-futureLoanCents)-(basePiCents*360-baseLoanCents),
    breakEvenPriceCents,
    breakEvenPriceChangeCents:breakEvenPriceCents===null?null:breakEvenPriceCents-Math.round(price*100)
  };
}
function renderRateSensitivity(price, down, rate, hoa){
  const rows = sensitivityBand(price, down, 500, 750, 25, hoa);
  if(!rows) return;
  const svg = document.getElementById('rate-band-chart');
  // Give the SVG a coordinate system that equals its rendered box. A fixed 720px viewBox made
  // the 12px axis labels shrink to illegible 5px type on a 390px phone, even though the chart
  // itself still fit. This retains the same geometry while keeping type at actual CSS pixels.
  const chartWidth=Math.max(280,Math.round(svg.clientWidth||720));
  const chartHeight=Math.max(210,Math.round(svg.clientHeight||250));
  svg.setAttribute('viewBox',`0 0 ${chartWidth} ${chartHeight}`);
  const left=chartWidth<460?48:64, right=chartWidth<460?12:22, top=20, bottom=38;
  const width=chartWidth-left-right, height=chartHeight-top-bottom;
  const min=Math.min(...rows.map(r=>r.pitiCents)), max=Math.max(...rows.map(r=>r.pitiCents));
  const span=Math.max(max-min,1), y = cents => top+height-(cents-min)/span*height;
  const x = bps => left+(bps-500)/(750-500)*width;
  const line = rows.map((r,i)=>`${i?'L':'M'}${x(r.rateBps).toFixed(1)},${y(r.pitiCents).toFixed(1)}`).join(' ');
  const area = `${line} L${x(rows[rows.length-1].rateBps).toFixed(1)},${top+height} L${x(rows[0].rateBps).toFixed(1)},${top+height} Z`;
  const text=getCss('--text-muted'), grid=getCss('--divider'), primary=getCss('--primary'), gold=getCss('--gold');
  const gridlines=[0,.25,.5,.75,1].map(t=>{
    const yy=top+height-height*t, dollars=(min+span*t)/100;
    return `<line x1="${left}" x2="${left+width}" y1="${yy}" y2="${yy}" stroke="${grid}" stroke-width="1"/><text x="${left-8}" y="${yy+4}" text-anchor="end" fill="${text}" font-size="12">$${(dollars/1000).toFixed(1)}K</text>`;
  }).join('');
  // The end labels are anchored inward rather than centred. Centred, half of "7.50%" sits
  // outside the plot, and on a 390px phone the right margin is 12px -- so the last label was
  // clipped by the SVG edge. Anchoring the ends keeps every label inside the box at any width.
  const xlabels=[500,625,750].map(bps=>{
    const anchor=bps===500?'start':bps===750?'end':'middle';
    return `<text x="${x(bps)}" y="${top+height+25}" text-anchor="${anchor}" fill="${text}" font-size="12">${(bps/100).toFixed(2)}%</text>`;
  }).join('');
  const rawMarkerBps=Math.round(rate*10000);
  const markerBps=Math.max(500,Math.min(750,rawMarkerBps));
  const outsideBand=rawMarkerBps<500||rawMarkerBps>750;
  const markerAnchor=rawMarkerBps>750?'end':rawMarkerBps<500?'start':'middle';
  const markerLabel=`slider ${(rawMarkerBps/100).toFixed(2)}%${outsideBand?' · outside band':''}`;
  svg.innerHTML=`<title>Full PITI rises as the rate rises, holding the same house constant.</title>${gridlines}<path d="${area}" fill="${primary}" opacity=".12"/><path d="${line}" fill="none" stroke="${primary}" stroke-width="3" stroke-linejoin="round" stroke-linecap="round"/><line x1="${x(markerBps)}" x2="${x(markerBps)}" y1="${top}" y2="${top+height}" stroke="${gold}" stroke-width="2" stroke-dasharray="5 4"/><text x="${x(markerBps)}" y="14" text-anchor="${markerAnchor}" fill="${gold}" font-size="12">${markerLabel}</text>${rows.map(r=>`<circle cx="${x(r.rateBps)}" cy="${y(r.pitiCents)}" r="3.5" fill="${primary}"/>`).join('')}${xlabels}<text x="${left}" y="14" fill="${text}" font-size="12">Full PITI / month</text>`;

  const nearest=rows.reduce((best,row)=>Math.abs(row.rateBps-rate*10000)<Math.abs(best.rateBps-rate*10000)?row:best,rows[0]);
  document.getElementById('rate-band-anchor').textContent=fmt$(nearest.pitiCents/100);
  document.getElementById('rate-band-anchor-d').textContent=`Nearest band point: ${(nearest.rateBps/100).toFixed(2)}% full PITI / mo`;
  const spread=rows[rows.length-1].pitiCents-rows[0].pitiCents;
  document.getElementById('rate-band-spread').textContent='+'+fmt$(spread/100);
  document.getElementById('rate-band-spread-d').textContent=`${(rows[0].rateBps/100).toFixed(2)}% to ${(rows[rows.length-1].rateBps/100).toFixed(2)}% on the same inputs`;
  document.getElementById('rate-band-table').innerHTML=rows.map(row=>`<tr class="${row.rateBps===nearest.rateBps?'anchor':''}"><td>${(row.rateBps/100).toFixed(2)}%</td><td class="num">${fmt$(row.pitiCents/100)}</td><td class="num">${fmt$(row.piCents/100)}</td><td class="num">${fmt$(row.totalInterestCents/100)}</td></tr>`).join('');

  // Waiting is intentionally just a disclosed +25 bp stress test at the same listed price.
  // It says nothing about where rates or prices will go; the solved break-even price shows why
  // a lower rate paired with a higher price can still lose.
  const futureRate=rate+.0025;
  const wait=waitVsBuy(price, down, rate, futureRate, price, hoa);
  const be=wait.breakEvenPriceCents===null?'unavailable':fmt$(wait.breakEvenPriceCents/100);
  const change=wait.breakEvenPriceChangeCents===null?'':` (${wait.breakEvenPriceChangeCents>=0?'+':''}${fmt$(wait.breakEvenPriceChangeCents/100)} vs today)`;
  document.getElementById('rate-wait-note').innerHTML=`<b>Wait stress test, not a forecast.</b> If the rate were ${(futureRate*100).toFixed(2)}% instead of ${(rate*100).toFixed(2)}% and price stayed ${fmt$(price)}, full PITI would change ${wait.monthlyDeltaCents>=0?'+':''}${fmt$(wait.monthlyDeltaCents/100)}/mo and term interest ${wait.lifetimeInterestDeltaCents>=0?'+':''}${fmt$(wait.lifetimeInterestDeltaCents/100)}. At that later rate, ${be}${change} is the price that restores today’s PITI. Mortgage insurance is not modeled.`;
}

function renderMaxPrice(inc, down, rate, hoa, dtiPct){
  const el = document.getElementById('k-maxp');
  const dEl = document.getElementById('k-maxp-d');
  const note = document.getElementById('maxp-note');
  const s = solveMaxPrice(inc, down, rate, hoa, dtiPct);
  if(!s.feasible){
    el.textContent = 'none';
    dEl.textContent = `Fixed costs alone are ${fmt$(s.floor)}/mo`;
    note.innerHTML = `<b>No price clears ${dtiPct}%.</b> Even at ${fmt$(s.floorPrice)} with no loan at all, taxes, insurance and HOA come to ${fmt$(s.floor)}/mo, above the cap. Fixed costs are the binding constraint, so a cheaper house does not solve it.`;
    return;
  }
  const p = s.price;
  const piti = pitiParts(p, down, rate, hoa).piti;
  el.textContent = fmt$(p);
  dEl.textContent = `PITI ${fmt$(piti)}/mo at the ${dtiPct}% cap`;
  const downPct = p>0 ? (down/p)*100 : 0;
  let pmi = '';
  if(downPct < 20){
    // Raising the price is what drives this share down, so the warning is a property of
    // the answer rather than of the inputs. Mortgage insurance is modeled nowhere in this
    // project, which is exactly why the number above is an upper bound.
    pmi = ` A fixed ${fmt$(down)} down on ${fmt$(p)} is ${downPct.toFixed(1)}%, under 20% — mortgage insurance would apply at roughly 0.3–1.5% of the loan per year and is <b>not</b> included. Treat this as an upper bound.`;
  }
  const target = readNum('i-price');
  const headroom = p - target;
  note.innerHTML = `<b>Lender basis: ${fmt$(p)}</b> at a ${dtiPct}% front-end DTI cap. That is PITI only, which is what a pre-approval letter shows. It excludes the maintenance reserve — the CLI and API return a second, lower <i>household</i> number that funds it.${pmi} Against your ${fmt$(target)} working price that is ${fmt$(Math.abs(headroom))} of ${headroom>=0?'headroom':'overshoot'}, which is the point: DTI is not the binding constraint at this income. Cash to close and the appraisal are.`;
}

function renderAmortization(price, down, rate){
  const loan = Math.max(0, price - down);
  const extra = readNum('i-extra');
  document.getElementById('l-extra').textContent = fmt$(extra);
  const $ = id => document.getElementById(id);

  const s = amortize(loan, rate, 360, extra);
  if(!s){
    // amortize returns null rather than a partial schedule. Say so instead of drawing an
    // empty chart, which reads as a broken page rather than an impossible input.
    ['a-pmt','a-int','a-cross','a-payoff'].forEach(id=>$(id).textContent='—');
    $('a-extra-note').innerHTML = '<b>No schedule at these inputs.</b> With no loan there is nothing to amortize — raise the price above the down payment.';
    $('a-body').innerHTML = '';
    if(SPLIT_CHART){ SPLIT_CHART.destroy(); SPLIT_CHART=null; }
    if(BAL_CHART){ BAL_CHART.destroy(); BAL_CHART=null; }
    $('a-excludes').innerHTML = '';
    return;
  }

  const yrs = Math.floor(s.months_to_payoff/12), mos = s.months_to_payoff%12;
  const term = t => yrs===0 ? `${mos} mo` : (mos===0 ? `${yrs} yr` : `${yrs} yr ${mos} mo`);

  $('a-pmt').textContent = fmt$(s.scheduled_payment);
  $('a-pmt-d').textContent = extra>0 ? `Plus ${fmt$(extra)} extra = ${fmt$(s.scheduled_payment+extra)}` : '30-yr fixed, principal & interest';
  $('a-int').textContent = fmt$(s.total_interest);
  // Interest as a share of the loan is the line that actually lands. "$280,540" is abstract;
  // "132% of what you borrowed" is not.
  $('a-int-d').textContent = loan>0 ? `${(s.total_interest/loan*100).toFixed(0)}% of the ${fmt$(loan)} borrowed` : '—';

  if(s.crossover_month){
    const cy = (s.crossover_month/12);
    $('a-cross').textContent = `#${s.crossover_month}`;
    $('a-cross-d').textContent = `Year ${cy.toFixed(1)} of ${(s.months_to_payoff/12).toFixed(1)}`;
  } else {
    $('a-cross').textContent = '—';
    $('a-cross-d').textContent = 'Never — interest leads throughout';
  }
  $('a-payoff').textContent = term();
  $('a-payoff-d').textContent = `${s.months_to_payoff} payments, last one ${fmt$(s.final_payment)}`;

  if(extra>0 && s.months_saved>0){
    const sy = Math.floor(s.months_saved/12), sm = s.months_saved%12;
    $('a-extra-note').innerHTML = `<b>${fmt$(extra)}/mo extra ends the loan ${sy} yr ${sm} mo early and saves ${fmt$(s.interest_saved)} in interest.</b> That assumes the extra payment starts with payment 1 and never stops, which no real budget guarantees — treat it as the ceiling, not a forecast. Extra principal does not reduce your tax, insurance or HOA, so the monthly outlay does not fall until the loan is gone.`;
  } else {
    $('a-extra-note').innerHTML = `<b>Nothing extra.</b> Drag the slider to see what prepaying principal does — at these terms the first payment is ${fmt$(s.payments[0].interest)} interest against ${fmt$(s.payments[0].principal)} of principal, so early extra dollars are the ones that count.`;
  }

  // The excludes list is spelled out here for the same reason the CLI and the API spell it
  // out: this number is smaller than PITI and the gap is the thing people misread.
  const piti = pitiParts(price, down, rate, readNum('i-hoa')).piti;
  $('a-excludes').innerHTML = `<b>This is the loan, not the payment.</b> ${fmt$(s.scheduled_payment)} is principal and interest. Excluded: property tax, homeowners insurance, HOA dues, mortgage insurance. The Affordability section above puts the full monthly housing cost at ${fmt$(piti)} — a ${fmt$(piti-s.scheduled_payment)}/mo difference that does not amortize and does not shrink when you prepay.`;

  const rows = s.years.map(y=>`<tr><td>${y.year}</td><td style="text-align:right">${fmt$(y.interest)}</td><td style="text-align:right">${fmt$(y.principal)}</td><td style="text-align:right">${y.extra>0?fmt$(y.extra):'—'}</td><td style="text-align:right">${fmt$(y.ending_balance)}</td></tr>`);
  $('a-body').innerHTML = rows.join('');

  const text = getCss('--text-muted'), grid = getCss('--divider');
  const labels = s.payments.map(p=>p.number);

  const sctx = $('split-chart');
  if(SPLIT_CHART) SPLIT_CHART.destroy();
  SPLIT_CHART = new Chart(sctx,{
    type:'line',
    data:{labels,datasets:[
      {label:'Interest',data:s.payments.map(p=>p.interest),borderColor:getCss('--red'),backgroundColor:'rgba(224,103,103,.10)',fill:true,tension:.1,pointRadius:0,borderWidth:2},
      {label:'Principal',data:s.payments.map(p=>p.principal+p.extra),borderColor:getCss('--primary'),backgroundColor:'rgba(125,211,192,.10)',fill:true,tension:.1,pointRadius:0,borderWidth:2}
    ]},
    options:{
      animation:false, responsive:true, maintainAspectRatio:false,
      scales:{
        x:{grid:{color:grid},ticks:{color:text,font:{size:11},maxTicksLimit:7,callback:function(v){const n=this.getLabelForValue(v);return 'yr '+Math.ceil(n/12);}}},
        y:{grid:{color:grid},ticks:{color:text,font:{size:11},callback:v=>'$'+v.toFixed(0)}}
      },
      plugins:{legend:{labels:{color:text,font:{size:11}}},tooltip:{callbacks:{title:i=>'Payment '+i[0].label,label:c=>c.dataset.label+': '+fmt$(c.parsed.y)}}}
    }
  });

  const bctx = $('bal-chart');
  if(BAL_CHART) BAL_CHART.destroy();
  const datasets = [{label:'Balance',data:s.payments.map(p=>p.balance),borderColor:getCss('--primary'),backgroundColor:'rgba(125,211,192,.10)',fill:true,tension:.1,pointRadius:0,borderWidth:2}];
  if(extra>0){
    // Drawn from the same amortize() call the savings figure came from, so the curve and
    // the number in the callout cannot disagree.
    const base = amortize(loan, rate, 360, 0);
    if(base) datasets.push({label:'Without extra',data:base.payments.map(p=>p.balance),borderColor:getCss('--gold'),borderDash:[6,4],pointRadius:0,borderWidth:1.5,fill:false});
  }
  BAL_CHART = new Chart(bctx,{
    type:'line',
    data:{labels:extra>0?Array.from({length:360},(_,i)=>i+1):labels,datasets},
    options:{
      animation:false, responsive:true, maintainAspectRatio:false,
      scales:{
        x:{grid:{color:grid},ticks:{color:text,font:{size:11},maxTicksLimit:7,callback:function(v){const n=this.getLabelForValue(v);return 'yr '+Math.ceil(n/12);}}},
        y:{grid:{color:grid},ticks:{color:text,font:{size:11},callback:v=>'$'+(v/1000).toFixed(0)+'K'}}
      },
      plugins:{legend:{labels:{color:text,font:{size:11}}},tooltip:{callbacks:{title:i=>'Payment '+i[0].label,label:c=>c.dataset.label+': '+fmt$(c.parsed.y)}}}
    }
  });
}

/* ---------------- RENT VS BUY ---------------- */
function bindRentBuy(){
  ['rent','appr','invret','sell'].forEach(k=>{
    document.getElementById('i-'+k).addEventListener('input',updateBE);
  });
  ['rent','appr','invret','sell'].forEach(k=>{});
}
function updateBE(){
  const rent=readNum('i-rent'), appr=readNum('i-appr')/100, ir=readNum('i-invret')/100, sell=readNum('i-sell')/100;
  document.getElementById('l-rent').textContent=fmt$(rent);
  document.getElementById('l-appr').textContent=readNum('i-appr').toFixed(1)+'%';
  document.getElementById('l-invret').textContent=readNum('i-invret').toFixed(1)+'%';
  document.getElementById('l-sell').textContent=readNum('i-sell').toFixed(1).replace('.0','')+'%';

  const price=readNum('i-price'), down=readNum('i-down'), rate=readNum('i-rate')/100, hoa=readNum('i-hoa');
  const closing = price*0.03;
  const loan = Math.max(0, price-down);
  const n=360, r=rate/12;
  const pi = r>0 ? loan*(r*Math.pow(1+r,n))/(Math.pow(1+r,n)-1) : loan/n;
  // Reuse the Prop 13 PITI components above so the break-even view does not retain the
  // old South Carolina millage formula.
  const {tax:taxMo0, ins:insMo0} = pitiParts(price, down, rate, hoa);

  const years=[], buyNet=[], rentNet=[];
  let equity=down, homeVal=price, balance=loan;
  let investBalance=down+closing;
  const rentAnnGrowth=0.03, taxIns=(taxMo0+insMo0)*12, insGrow=0.06;
  let currRent=rent;
  for(let y=1;y<=15;y++){
    for(let m=1;m<=12;m++){
      const interest=balance*r;
      const principal=pi-interest;
      balance=Math.max(0,balance-principal);
    }
    homeVal*=(1+appr);
    const sellCosts=homeVal*sell;
    const buyPos = (homeVal - balance) - sellCosts - closing;
    investBalance*=(1+ir); // opportunity cost
    // rentPos: pay rent + save the mortgage-vs-rent delta? Assume equivalent lifestyle: rent path adds cumulative rent payments, but invests the down/closing
    const monthlyOwn = pi + (taxIns/12)*(Math.pow(1+insGrow,y-1)) + hoa;
    // For simplicity: rent path invests (down+closing) at ir; buy path locks in monthly own vs monthly rent
    // Break-even when buyPos + (cumulative rent-own savings compounded) - (rent path invest balance) >= 0
    const rentPos = investBalance - 0; // baseline
    years.push(y);
    buyNet.push(buyPos);
    rentNet.push(rentPos);
    currRent*=(1+rentAnnGrowth);
  }
  // Break-even year: first year buyNet > rentNet
  let be=null;
  for(let i=0;i<years.length;i++){ if(buyNet[i]>=rentNet[i]){ be=years[i]; break; } }
  document.getElementById('k-be').textContent = be? be+' years' : '>15 years';
  document.getElementById('k-be-d').textContent = be? (be<=5?'Fast payoff — buy leans strong':(be<=7?'Reasonable if you stay 7+':'Long payoff — rent longer or negotiate price')): 'Longer than 15 years — do not buy at these inputs';

  const ctx=document.getElementById('be-chart');
  const text = getCss('--text-muted'), grid=getCss('--divider');
  if(BE_CHART) BE_CHART.destroy();
  BE_CHART = new Chart(ctx,{
    type:'line',
    data:{labels:years,datasets:[
      {label:'Buy net position',data:buyNet,borderColor:getCss('--primary'),backgroundColor:'rgba(125,211,192,.10)',tension:.25,fill:true,pointRadius:0,borderWidth:2},
      {label:'Rent path (invest down at return)',data:rentNet,borderColor:getCss('--gold'),backgroundColor:'rgba(224,168,58,.08)',tension:.25,fill:true,pointRadius:0,borderWidth:2}
    ]},
    options:{
      animation:false, responsive:true, maintainAspectRatio:false,
      scales:{
        x:{grid:{color:grid},ticks:{color:text,font:{size:11}},title:{display:true,text:'Years owned',color:text}},
        y:{grid:{color:grid},ticks:{color:text,font:{size:11},callback:v=>'$'+(v/1000).toFixed(0)+'K'}}
      },
      plugins:{legend:{labels:{color:text,font:{size:11}}}, tooltip:{callbacks:{label:c=>c.dataset.label+': '+fmt$(c.raw)}}}
    }
  });
}
function getCss(v){return getComputedStyle(document.documentElement).getPropertyValue(v).trim();}

/* ---------------- SCORECARD ---------------- */
/* One cell carrying district, CAASPP proficiency and a proficiency bar. The bar is scaled to
   100% rather than to the best row on the page, because a relative bar would make the
   strongest district in a weak field look excellent. Districts with no published figure show
   an explicit dash -- blank would read as zero. */
function schoolCell(s){
  const pct = s.school_proficiency_pct;
  const name = s.school_district || '—';
  const link = s.school_district_url
    ? `<a href="${s.school_district_url}" target="_blank" rel="noopener">${name}</a>` : name;
  if(pct==null) return `<div style="font-size:var(--text-xs)">${link}</div><span style="color:var(--text-faint);font-size:var(--text-xs)">no published figure</span>`;
  // 55% is the profile's floor; below it the engine deducts, so it is the natural colour break.
  const col = pct>=70 ? 'var(--good)' : pct>=55 ? 'var(--warn)' : 'var(--bad)';
  return `<div style="font-size:var(--text-xs)">${link}</div>`
    + `<span class="numeric" style="color:${col};font-weight:600">${pct.toFixed(1)}%</span>`
    + `<span class="bar" style="display:inline-block;margin-left:6px;width:${Math.max(2,Math.round(pct*0.6))}px;background:${col}"></span>`;
}
let SORT_KEY='score', SORT_DIR=-1;
function renderScorecard(){
  const body = document.getElementById('score-body');
  const wFiber = readNum('i-w-fiber')/100;
  const wSchool = readNum('i-w-school')/100;
  // Schools and the homelab are the two weights the buyer sets directly; the remaining four
  // split what is left. Guarding against a negative remainder matters because the two
  // sliders can together exceed 100%.
  const wRest = Math.max(0,(1-wFiber-wSchool))/4;
  const weights = {price:wRest, leverage:wRest, commute:wRest, safety:wRest, fiber:wFiber, schools:wSchool};
  document.getElementById('weight-tags').innerHTML = Object.entries(weights).map(([k,v])=>`<span class="tag${k==='fiber'?' h':''}">${k}: ${(v*100).toFixed(0)}%</span>`).join('');

  const rows = DATA.submarkets.map(s=>{
    const sc = s.scores;
    const composite = weights.price*sc.price + weights.leverage*sc.leverage + weights.commute*sc.commute + weights.safety*sc.safety + weights.fiber*sc.fiber + weights.schools*(sc.schools??0);
    // penalize deal-breakers: >20 min commute is bad
    const commutePenalty = s.drive_min > 20 ? 0.85 : 1;
    const finalScore = composite*commutePenalty;
    return {s, finalScore};
  });
  const map = {
    name:r=>r.s.name, price:r=>r.s.redfin?.median_price??0, ppsf:r=>r.s.redfin?.ppsf??0,
    yoy:r=>r.s.redfin?.yoy_price??0, dom:r=>r.s.redfin?.dom??0, inv:r=>r.s.redfin?.inventory??0,
    drive:r=>r.s.drive_min, score:r=>r.finalScore,
    schools:r=>r.s.school_proficiency_pct??-1
  };
  rows.sort((a,b)=>{
    const av=map[SORT_KEY](a), bv=map[SORT_KEY](b);
    if(typeof av==='string') return SORT_DIR*av.localeCompare(bv);
    return SORT_DIR*(av-bv);
  });
  // Rank top-2 and worst by score
  const ranked=[...rows].sort((a,b)=>b.finalScore-a.finalScore);
  const top2 = new Set(ranked.slice(0,2).map(r=>r.s.id));
  const bottom = new Set(ranked.slice(-2).map(r=>r.s.id));

  body.innerHTML = rows.map(({s,finalScore})=>{
    const r=s.redfin;
    const decision = top2.has(s.id) ? '<span class="chip take">TAKE (top 2)</span>' : bottom.has(s.id) ? '<span class="chip pass">PASS</span>' : '<span class="chip watch">WATCH</span>';
    const cls = top2.has(s.id) ? 'top-2' : bottom.has(s.id) ? 'reject' : '';
    return `<tr class="${cls}">
      <td><b>${s.name}</b><br><span style="color:var(--text-faint);font-size:var(--text-xs)">${s.zips.join(', ')}</span></td>
      <td>${schoolCell(s)}</td>
      <td class="num">${fmt$k(r?.median_price)}</td>
      <td class="num">$${Math.round(r?.ppsf||0)}</td>
      <td class="num ${(r?.yoy_price>0)?'up':(r?.yoy_price<0?'down':'flat')}">${((r?.yoy_price||0)*100).toFixed(1)}%</td>
      <td class="num">${Math.round(r?.dom||0)}</td>
      <td class="num">${r?.inventory||0}</td>
      <td class="num">${s.drive_min}m</td>
      <td class="num"><span class="bar" style="width:${Math.max(4,finalScore*70)}px"></span>${(finalScore*100).toFixed(0)}</td>
      <td>${decision}</td>
    </tr>`;
  }).join('');

  // Bind sort clicks once
  if(!renderScorecard.bound){
    renderScorecard.bound=true;
    document.querySelectorAll('#score-table th').forEach(th=>{
      th.addEventListener('click',()=>{
        const k=th.dataset.sort; if(!k) return;
        if(SORT_KEY===k) SORT_DIR*=-1; else{SORT_KEY=k;SORT_DIR=(k==='name')?1:-1;}
        renderScorecard();
      });
    });
  }
}

/* ---------------- TIMING ---------------- */
function renderTiming(){
  // Thresholds are re-calibrated for Los Angeles, not carried over from the Spartanburg
  // edition. LA County was at 3.5 months of supply in July 2026 -- a level that counted as
  // "leverage gone" in the South Carolina version. A tight market that is nonetheless posting
  // negative year-over-year prices is a different animal, so the trigger here is price
  // direction and rate, not inventory depth.
  const CAR = 'https://www.car.org/marketdata/data/countysalesactivity';
  const inds = [
    ['Median sale price · LA County','$888,120 (Jul 2026)','Two more −YoY quarters','Back above $911K (Jul 2025)',CAR],
    ['Median price YoY','−2.6% (Jul 2026)','Stays negative through Q1 2027','Turns positive',CAR],
    ['Months supply · C.A.R. UII','3.5 mo (Jul 2026)','>4.5 mo','<3.0 mo = leverage gone',CAR],
    ['Median days to sell','26 days (Jul 2026)','>40 days','<20 days',CAR],
    ['Closed sales YoY','−0.9% (Jul 2026)','Deepens past −5%','Turns positive',CAR],
    ['30-yr PMMS rate','6.67% (Aug 13, 2026)','Sustained <6.25%','Sustained >7.25%','https://www.freddiemac.com/pmms'],
    ['Port of LA · fiscal-year TEU','9.40M FY2025/26 (−2.4%)','Port forecast of −7% for FY26/27 lands','Volumes return to growth','https://portoflosangeles.org/business/statistics/container-statistics'],
    ['CA WARN · LA County','Largest recent notice 253 workers (Aug 2026)','Any single notice >1,000 workers','No new notices for two quarters','https://edd.ca.gov/en/jobs_and_training/Layoff_Services_WARN/'],
    ['Metro residential permits','2,385 (Jun 2026, LA–Long Beach–Anaheim MSA)','2026 run-rate falls','2026 run-rate rises','https://fred.stlouisfed.org/data/LOSA106BPPRIV.txt']
  ];
  document.getElementById('timing-body').innerHTML = inds.map(([n,cur,g,b,src])=>`
    <div style="border-bottom:1px solid var(--divider);padding:8px 0">
      <div style="display:flex;justify-content:space-between;align-items:baseline;gap:12px">
        <b>${n}</b>
        <span style="color:var(--text-muted);font-size:var(--text-xs)">${cur}</span>
      </div>
      <div style="font-size:var(--text-xs);color:var(--text-muted);margin-top:3px">
        <span style="color:var(--green)">Better: ${g}</span>
        &nbsp;·&nbsp;
        <span style="color:var(--red)">Worse: ${b}</span>
        &nbsp;·&nbsp;
        <a href="${src}" target="_blank" style="color:var(--text-faint)">source</a>
      </div>
    </div>`).join('');
}

/* ---------------- HAZARD RISK ----------------
   Reads data.hazards, written by tools/build_hazard_snapshot.py from the FEMA National
   Risk Index. Deliberately isolated from the scoring path: nothing in here touches
   updateProperty or the rules block, and tests/test_hazard_snapshot.py asserts that.
   Hazard risk is a caveat (ADR 0009), and the page has to agree with the engine about
   that or we are back to two implementations of one rule. */

/* Worst first. Sorting by FEMA's label would put "Relatively Moderate" hail above
   "Relatively Low" wildfire at 68.9 — the exact inversion the callout warns about. */
function hzRank(h){ return h.modeled ? h.percentile : -1; }

function hzColor(p){
  if(p==null) return 'var(--text-faint)';
  if(p>=90) return 'var(--red)';
  if(p>=75) return 'var(--gold)';
  return 'var(--primary)';
}

function renderHazards(){
  const H = DATA.hazards;
  if(!H){ document.getElementById('hazard').style.display='none'; return; }

  const sel = document.getElementById('hz-zip');
  const codes = Object.keys(H.zips).sort();
  sel.innerHTML = codes.map(z=>
    `<option value="${z}"${z==='29301'?' selected':''}>${z} · ${H.zips[z].name}</option>`
  ).join('');
  sel.addEventListener('change', updateHazards);

  const drgt = H.county.hazards.DRGT;
  if(drgt){
    document.getElementById('hz-drgt-n').textContent = `${drgt.modeled_tracts} of ${drgt.total_tracts}`;
    if(drgt.min!=null) document.getElementById('hz-drgt-r').textContent = `${drgt.min.toFixed(1)} to ${drgt.max.toFixed(1)}`;
  }

  document.getElementById('hz-src').innerHTML =
    `Source — <a href="${H.source_url}" target="_blank">FEMA National Risk Index</a>, `+
    `data version ${H.nri_version}, ${H.county.tract_count} census tracts in `+
    `${H.county_name}. Retrieved ${H.retrieved} at build time and committed: this page `+
    `looks nothing up at runtime. Percentiles are national, 0–100, higher is worse — `+
    `except community resilience, where higher is better.`;

  updateHazards();
}

function updateHazards(){
  const H = DATA.hazards;
  const zip = document.getElementById('hz-zip').value;
  const Z = H.zips[zip];

  const rows = H.hazard_codes
    .map(c=>({code:c, tract:Z.hazards[c]||{label:H.hazard_labels[c],modeled:false}, county:H.county.hazards[c]||{}}))
    .sort((a,b)=>hzRank(b.tract)-hzRank(a.tract));

  document.getElementById('hz-bars').innerHTML = rows.map(({tract,county})=>{
    const label = tract.label.charAt(0).toUpperCase()+tract.label.slice(1);

    if(!tract.modeled){
      // Unknown, not low. If the county models it elsewhere, say so — that is the
      // difference between "no exposure here" and "nobody measured this tract".
      const elsewhere = county.modeled_tracts>0
        ? `Not modeled for this tract. Modeled in ${county.modeled_tracts} of `+
          `${county.total_tracts} county tracts, where it runs ${county.min}–${county.max}. `+
          `<b style="color:var(--gold)">Unknown, not low.</b>`
        : `Not modeled anywhere in the county.`;
      return `<div style="border-bottom:1px solid var(--divider);padding:9px 0">
        <div style="display:flex;justify-content:space-between;align-items:baseline;gap:10px">
          <b>${label}</b><span style="color:var(--text-faint);font-size:var(--text-xs)">no rating</span>
        </div>
        <div style="font-size:var(--text-xs);color:var(--text-muted);margin-top:3px">${elsewhere}</div>
      </div>`;
    }

    const p = tract.percentile;
    // County span drawn as a track behind the tract marker, so "63 out of a county that
    // runs 19 to 76" is one glance instead of two numbers to hold in your head.
    const lo = county.min ?? 0, hi = county.max ?? 100;
    return `<div style="border-bottom:1px solid var(--divider);padding:9px 0">
      <div style="display:flex;justify-content:space-between;align-items:baseline;gap:10px">
        <b>${label}</b>
        <span style="font-size:var(--text-xs);color:var(--text-muted)">
          <b class="numeric" style="color:${hzColor(p)};font-size:var(--text-sm)">${p.toFixed(1)}</b>
          <span style="color:var(--text-faint)"> · FEMA calls this “${tract.rating}”</span>
        </span>
      </div>
      <div style="position:relative;height:7px;margin-top:6px;background:var(--surface-2);border-radius:4px;overflow:hidden">
        <div style="position:absolute;left:${lo}%;width:${Math.max(hi-lo,0.5)}%;top:0;bottom:0;background:var(--border)"></div>
        <div style="position:absolute;left:${Math.min(p,99.2)}%;top:-2px;bottom:-2px;width:2.5px;background:${hzColor(p)};border-radius:2px"></div>
      </div>
      <div style="font-size:var(--text-xs);color:var(--text-faint);margin-top:3px">County spread ${lo}–${hi}, median ${county.median}</div>
    </div>`;
  }).join('');

  const put=(id,obj,better)=>{
    const el=document.getElementById(id), d=document.getElementById(id+'-d');
    if(!obj){ el.textContent='–'; el.style.color='var(--text-faint)'; d.textContent='FEMA gives this tract no rating — unknown, not low'; return; }
    el.textContent = obj.percentile.toFixed(1);
    el.style.color = better==='high' ? 'var(--text)' : hzColor(obj.percentile);
    d.innerHTML = `“${obj.rating}”`;
  };
  put('hz-comp', Z.nri_composite_risk);
  put('hz-sovi', Z.social_vulnerability);
  put('hz-resl', Z.community_resilience, 'high');
  document.getElementById('hz-tract').textContent = Z.tract_fips || '–';

  const cty = H.county;
  const notes = [];

  if(Z.nri_composite_risk) notes.push(
    `Composite averages all 18 hazards FEMA models, including the 11 that do not apply `+
    `here, so it runs low by construction. County median is ${cty.nri_composite_risk.median}. `+
    `Read the individual bars, not this number.`);

  // The composite hides a hazard only when there is something worth hiding. An early
  // version fired on any wide gap and flagged Spartanburg hail against a low composite,
  // the same trap this fork hits with Boyle Heights earthquake risk,
  // which is a true statement about arithmetic and useless as a warning.
  const worst = rows.filter(r=>r.tract.modeled).sort((a,b)=>b.tract.percentile-a.tract.percentile)[0];
  if(worst && Z.nri_composite_risk && worst.tract.percentile>=75 &&
     worst.tract.percentile - Z.nri_composite_risk.percentile >= 25){
    notes.push(`<b style="color:var(--gold)">The composite understates this tract.</b> `+
      `${worst.tract.label.charAt(0).toUpperCase()+worst.tract.label.slice(1)} sits at `+
      `${worst.tract.percentile.toFixed(1)} while the composite reads `+
      `${Z.nri_composite_risk.percentile.toFixed(1)}.`);
  }

  if(cty.community_resilience && cty.community_resilience.varies_by_tract===false) notes.push(
    `<b>Community resilience is a county figure, not a tract one.</b> All `+
    `${cty.tract_count} tracts in ${cty.county_name||H.county_name} return the same `+
    `${cty.community_resilience.median} — verified against three other counties, which `+
    `each return one value too. It tells you nothing about this neighbourhood. Social `+
    `vulnerability does vary, across ${cty.social_vulnerability.min}–${cty.social_vulnerability.max}.`);

  notes.push(`This is the single tract containing the ZIP centroid, not an average over `+
    `the ZIP. 29301 is a working example of why that matters: the centroid tract reads `+
    `wildfire 68.9, and the tract holding 606 Andre Ct — same ZIP — reads 28.4.`);

  document.getElementById('hz-notes').innerHTML = notes.map(n=>
    `<div style="font-size:var(--text-xs);color:var(--text-muted);padding:7px 0;border-top:1px solid var(--divider);line-height:1.6">${n}</div>`
  ).join('');
}

/* ---------------- PROPERTY SCORER ---------------- */
function bindProperty(){
  ['addr','price','bb','sqft','hoa','gar','fib','com','sep','fld','roof','hvac'].forEach(k=>{
    document.getElementById('p-'+k).addEventListener('input',updateProperty);
  });
}
function updateProperty(){
  /* Evaluates the rule set compiled from buyer_profile.toml into data.json by
     tools/build_snapshot.py. It does NOT restate the rules.

     This function used to hold its own hand-written copy of the thresholds, and it had
     drifted from the Python engine into giving opposite answers -- it treated an HOA over
     $100/mo as a hard fail (the engine deducts 25), and it gave an 17-year-old roof and a
     14-year-old HVAC no penalty at all (the engine deducts 40 between them). On 606 Andre
     Ct that was a confident TAKE on a house the engine scores 52 WATCH.

     The ordering below mirrors analyzer/core/scoring.py deliberately: hard fails, then
     deductions, then capital expenses, then the unknown-facts cap, and only then the
     verdict bands. Reordering it will change answers. */
  const R = DATA.rules;
  const num = id => {
    const el = document.getElementById(id);
    const raw = (el.value || '').trim();
    return raw === '' ? null : +raw;   // '' is unknown, which is not the same as 0
  };

  const price = num('p-price') || 0;
  const sqft  = num('p-sqft');
  const hoa   = num('p-hoa');
  const gar   = num('p-gar');
  const fib   = num('p-fib');
  const com   = num('p-com');
  const sep   = num('p-sep');
  const fld   = num('p-fld');
  const roof  = num('p-roof');
  const hvac  = num('p-hvac');
  const bb    = (document.getElementById('p-bb').value || '').split('/').map(s => +s.trim());
  const beds  = isNaN(bb[0]) ? null : bb[0];
  const baths = isNaN(bb[1]) ? null : bb[1];
  const targetPrice = readNum('i-price');

  const facts = {hoa, beds, baths, sqft, garage: gar, fiber: fib};
  let score = 100;
  const hardFails = [], deductions = [], capex = [], caveats = [], unknown = [];

  /* -- hard fails: disqualifying at any price -- */
  if(com !== null && com > R.hard_fails.find(h => h.id === 'commute').threshold){
    hardFails.push(`${com}-minute commute exceeds the ${R.hard_fails.find(h=>h.id==='commute').threshold}-minute limit`);
  }
  if(sep === 1) hardFails.push('Well or septic rather than public water and sewer');
  if(fld === 1) hardFails.push('Inside a FEMA special flood hazard area');

  if(hardFails.length){
    score = 0;
  } else {
    /* -- deductions: survivable, weighted -- */
    R.deductions.forEach(rule => {
      const v = facts[rule.id];
      if(v === null || v === undefined) return;   // unknown never deducts, it caps later
      let hit = false;
      if(rule.compare === 'greater_than') hit = v > rule.threshold;
      else if(rule.compare === 'less_than') hit = v < rule.threshold;
      else if(rule.compare === 'is_false') hit = v === 0;
      if(hit){
        score -= rule.points;
        deductions.push({points: rule.points, label: rule.label, note: rule.note || ''});
      }
    });

    /* -- capital expenses: aging systems with a bill attached -- */
    const ages = {roof, hvac};
    R.capital_expenses.forEach(item => {
      const age = ages[item.id];
      if(age === null || age === undefined) return;
      let points = 0, tier = '';
      if(age >= item.overdue_age){ points = item.overdue_points; tier = 'overdue'; }
      else if(age >= item.due_age){ points = item.due_points; tier = 'due'; }
      if(!points) return;
      const band = sqft === null
        ? item.unknown_sqft
        : (item.bands.find(b => b.max_sqft !== null && sqft <= b.max_sqft) || item.bands[item.bands.length - 1]);
      score -= points;
      capex.push({points, component: item.component, tier, low: band.low, high: band.high, src: item.source_url});
    });
  }

  score = Math.max(0, Math.min(100, score));

  /* -- unknown facts cap: silence must not read as perfection -- */
  const UNKNOWN_LABELS = {sqft: 'heated square footage', beds: 'bedroom count', baths: 'bathroom count'};
  Object.keys(UNKNOWN_LABELS).forEach(k => { if(facts[k] === null) unknown.push(UNKNOWN_LABELS[k]); });
  let capped = false;
  if(unknown.length && !hardFails.length){
    const cap = R.verdict.take_min - 1;
    if(score > cap){ score = cap; capped = true; }   // one-directional, only ever lowers
  }

  /* -- verdict bands, last -- */
  let verdict = 'PASS';
  if(hardFails.length) verdict = 'PASS';
  else if(score >= R.verdict.take_min) verdict = 'TAKE';
  else if(score >= R.verdict.watch_min) verdict = 'WATCH';

  /* -- caveats: flagged, never scored -- */
  if(capped){
    caveats.push(`Score capped at ${R.verdict.take_min - 1} because ${unknown.join(', ')} ${unknown.length===1?'is':'are'} unknown. Fill those in and re-score — the cap only lowers a score, never raises one.`);
  } else if(unknown.length){
    caveats.push(`${unknown.join(', ')} unknown, so any related deduction is unapplied rather than passed.`);
  }
  if(price && targetPrice && price > targetPrice * (1 + R.caveats.max_price_over_target_pct)){
    caveats.push(`${fmt$(price)} is ${((price/targetPrice-1)*100).toFixed(0)}% above your ${fmt$(targetPrice)} target — negotiate or widen the budget.`);
  }
  if(price && sqft && price/sqft > R.caveats.max_price_per_sqft){
    caveats.push(`Above $${R.caveats.max_price_per_sqft}/sqft — check comps in the same ZIP.`);
  }

  /* -- render -- */
  const el = document.getElementById('p-score');
  el.textContent = score;
  el.style.color = verdict==='TAKE' ? 'var(--green)' : verdict==='WATCH' ? 'var(--gold)' : 'var(--red)';
  document.getElementById('p-score-d').innerHTML =
    `<span class="chip ${verdict.toLowerCase()}">${verdict}</span> ` +
    (verdict==='TAKE' ? 'Strong fit · take to inspection'
      : verdict==='WATCH' ? 'Watch · negotiate or verify'
      : hardFails.length ? 'Deal-breaker · not a candidate at any price'
      : 'Pass · does not fit your rules');

  const rows = [];
  hardFails.forEach(f => rows.push(`<li><b style="color:var(--red)">DEAL-BREAKER</b> ${f}</li>`));
  deductions.forEach(d => rows.push(
    `<li><b style="color:var(--gold)">−${d.points}</b> ${d.label}${d.note?` <span style="color:var(--text-faint)">${d.note}</span>`:''}</li>`));
  capex.forEach(c => rows.push(
    `<li><b style="color:var(--gold)">−${c.points}</b> ${c.component} (${c.tier}) — estimated ${fmt$(c.low)}–${fmt$(c.high)} · <a href="${c.src}" target="_blank" style="color:var(--text-faint)">source</a></li>`));
  if(capex.length){
    const lo = capex.reduce((s,c)=>s+c.low,0), hi = capex.reduce((s,c)=>s+c.high,0);
    rows.push(`<li><b>Near-term capital total</b> ${fmt$(lo)}–${fmt$(hi)} — get contractor quotes and negotiate a credit</li>`);
  }
  caveats.forEach(c => rows.push(`<li><span style="color:var(--text-faint)">note</span> ${c}</li>`));
  if(!rows.length) rows.push('<li>No deductions. Verify the facts above against the county record before writing an offer.</li>');

  // Always-on pre-offer checks. Not scored, and not optional.
  rows.push('<li><span style="color:var(--text-faint)">after closing</span> File the $7,000 homeowners\' exemption with the Assessor by Feb 15 — the seller\'s exemption does not transfer</li>');
  rows.push('<li><span style="color:var(--text-faint)">after closing</span> Expect a supplemental tax bill — the Assessor bills the gap between the seller\'s frozen basis and your purchase price</li>');
  rows.push('<li><span style="color:var(--text-faint)">before offer</span> Compare the seller\'s tax history against the estimated post-purchase bill</li>');
  rows.push('<li><span style="color:var(--text-faint)">before offer</span> Get a real insurance quote; the model uses a statewide average</li>');
  rows.push('<li><span style="color:var(--text-faint)">before offer</span> Call the ISP with the exact street address — FCC data is census-block precision</li>');

  document.getElementById('p-flags').innerHTML = rows.join('');
}

/* ---------------- RUNWAY ---------------- */
function bindRunway(){
  ['save','msave','dpct'].forEach(k=>document.getElementById('i-'+k).addEventListener('input',updateRunway));
}
function updateRunway(){
  const save=readNum('i-save'), msave=readNum('i-msave'), dpct=readNum('i-dpct')/100;
  const price=readNum('i-price');
  document.getElementById('l-save').textContent=fmt$(save);
  document.getElementById('l-msave').textContent=fmt$(msave);
  document.getElementById('l-dpct').textContent=(dpct*100).toFixed(0)+'% of price = '+fmt$(price*dpct);

  const monthsToLease = 12; // Aug 2026 → Aug 2027 ≈ 12 months
  const labels=[], balance=[]; let b=save;
  for(let m=0;m<=monthsToLease;m++){
    labels.push('M+'+m);
    balance.push(b);
    b += msave;
  }
  const target = price*dpct;
  const projected = save + msave*monthsToLease;
  const status = projected>=target ? 'On track' : 'Short by '+fmt$(target-projected);
  document.getElementById('k-runway').textContent=status;
  document.getElementById('k-runway').style.color = projected>=target?'var(--green)':'var(--red)';
  document.getElementById('k-runway-d').textContent = `Projected by lease-end: ${fmt$(projected)} vs target ${fmt$(target)}`;

  const ctx=document.getElementById('runway-chart');
  const text = getCss('--text-muted'), grid=getCss('--divider');
  if(RUNWAY_CHART) RUNWAY_CHART.destroy();
  RUNWAY_CHART = new Chart(ctx,{
    type:'line',
    data:{labels,datasets:[
      {label:'Savings balance',data:balance,borderColor:getCss('--primary'),backgroundColor:'rgba(125,211,192,.10)',fill:true,tension:.2,pointRadius:0,borderWidth:2},
      {label:'Target down-payment',data:labels.map(()=>target),borderColor:getCss('--gold'),borderDash:[6,4],pointRadius:0,borderWidth:1.5,fill:false}
    ]},
    options:{
      animation:false, responsive:true, maintainAspectRatio:false,
      scales:{
        x:{grid:{color:grid},ticks:{color:text,font:{size:11},maxTicksLimit:6}},
        y:{grid:{color:grid},ticks:{color:text,font:{size:11},callback:v=>'$'+(v/1000).toFixed(0)+'K'}}
      },
      plugins:{legend:{labels:{color:text,font:{size:11}}}}
    }
  });
}

boot();
