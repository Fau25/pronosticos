import json, urllib.request
B="https://site.api.espn.com/apis/site/v2/sports/soccer/{}/scoreboard?dates={}&limit=1000"
def leer(u):
    r=urllib.request.Request(u, headers={"User-Agent":"Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(r, timeout=40).read().decode())
out={}
for s in ["por.1","ned.1","bel.1","sco.1","tur.1","aut.1","gre.1","den.1","sui.1","cze.1","nor.1","ukr.1","srb.1","cro.1","cyp.1","pol.1","swe.1","isr.1","kaz.1","rus.1"]:
    try:
        d=leer(B.format(s,"20260901-20260930"))
        ev=d.get("events",[]); fin=[e for e in ev if e["status"]["type"].get("completed")]
        st=fin[0]["competitions"][0]["competitors"][0].get("statistics",[]) if fin else []
        det=len(fin[0]["competitions"][0].get("details",[])) if fin else 0
        out[s]=dict(nombre=d["leagues"][0]["name"], eventos=len(ev), jugados=len(fin), stats=[x["name"] for x in st], detalles=det,
                    equipos=sorted({c["team"]["displayName"] for e in ev for c in e["competitions"][0]["competitors"]})[:30])
    except Exception as e:
        out[s]=str(e)
json.dump(out, open("registro/sonda_ligas.json","w"), indent=1, ensure_ascii=False)
