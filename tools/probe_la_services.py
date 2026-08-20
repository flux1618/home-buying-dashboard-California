import requests
urls=[
'https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/SchoolDistricts_Current/MapServer',
'https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/State_County/MapServer',
'https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Tracts_Blocks/MapServer',
'https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Places_CouSub_ConCity_SubMCD/MapServer',
'https://hazards.fema.gov/gis/nfhl/rest/services/public/NFHL/MapServer',
'https://router.project-osrm.org/table/v1/driving/-118.203042,34.06238;-118.1,34.1?annotations=duration,distance&sources=1&destinations=0',
]
for u in urls:
 try:
  r=requests.get(u,params={'f':'json'} if '?' not in u else None,headers={'User-Agent':'hbd-ca-data-rebuild/1.0'},timeout=45)
  print('\nURL',r.url,'status',r.status_code,'len',len(r.content))
  d=r.json()
  print('name',d.get('mapName') or d.get('name'), 'layers',[(x.get('id'),x.get('name')) for x in d.get('layers',[])][:80], 'err',d.get('error'))
  if 'durations' in d: print(d)
 except Exception as e: print('ERR',u,repr(e))
