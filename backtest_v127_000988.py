# -*- coding: utf-8 -*-
"""华工科技 V1.27 信号引擎 & 大盘联动 回测 (V3)"""
import argparse, json, os, sys, time as _tm, urllib.request
from datetime import datetime, timedelta
import numpy as np, pandas as pd

TUSHARE_TOKEN = "9d15f39266cbbf8a1e5efa1525d7a4d4d1dbc62ec8cbce167d642def"
CODE = "000988"; NAME = "华工科技"
FQ = 200; MB = 3; MS = 3; IH = 1000; IC = 50000.0; CM=0.00015; ST=0.0005; SL=0.01
_OUT = os.path.join(os.path.dirname(__file__),"t_io","backtests","v127_000988")
_ts_pro = None
def _ts():
    global _ts_pro
    if _ts_pro is None:
        import tushare as _t; _ts_pro = _t.pro_api(TUSHARE_TOKEN)
    return _ts_pro
def _tcent(sym,end,cnt=600):
    sd=datetime.strptime(end,"%Y-%m-%d")-timedelta(days=int(cnt*1.6)+40)
    url=f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,{sd.strftime('%Y-%m-%d')},{end},{cnt},qfq"
    try:
        r=urllib.request.urlopen(urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0"}),timeout=15)
        js=json.loads(r.read().decode("utf-8",errors="ignore"))
        rows=js["data"][sym].get("qfqday") or js["data"][sym].get("day") or []
        recs=[]
        for row in rows:
            if not isinstance(row,(list,tuple)) or len(row)<6: continue
            try:
                a=float(row[6]) if len(row)>=7 else float(row[5])
                recs.append({"date":str(row[0])[:10],"open":float(row[1]),"close":float(row[2]),"high":float(row[3]),"low":float(row[4]),"volume":float(row[5]),"amount":a})
            except: continue
        if not recs: return None
        df=pd.DataFrame(recs).drop_duplicates(subset="date",keep="last").sort_values("date").reset_index(drop=True)
        for c in ["open","high","low","close"]: df[c]=pd.to_numeric(df[c],errors="coerce")
        for n in [5,10,20,60]: df[f"ma{n}"]=df["close"].rolling(n).mean()
        return df
    except: return None
# suppress tushare request logs
import logging as _lg; _lg.getLogger('tushare').setLevel(_lg.ERROR)
def _tsmin(code,start,end,label=""):
    d="".join(c for c in code if c.isdigit())[:6]; tc=f"{d}.{'SZ' if d.startswith(('0','3')) else 'SH'}"
    pro=_ts(); all_df=[]; cur=datetime.strptime(start,"%Y-%m-%d"); ed=datetime.strptime(end,"%Y-%m-%d")
    total_days=(ed-cur).days+1; fetched=0; _last_pct=-1
    while cur<=ed:
        ce=min(cur+timedelta(days=6),ed); s=cur.strftime("%Y-%m-%d 09:00:00"); e=ce.strftime("%Y-%m-%d 15:30:00")
        try:
            df=pro.stk_mins(ts_code=tc,freq="1min",start_date=s,end_date=e)
            if df is not None and not df.empty:
                df=df.rename(columns={"trade_time":"time","vol":"volume"}); df["time"]=pd.to_datetime(df["time"])
                h,m2=df["time"].dt.hour,df["time"].dt.minute
                df=df[((h==9)&(m2>=30))|(h==10)|((h==11)&(m2<=30))|((h==13)&(m2>=0))|(h==14)|((h==15)&(m2==0))].copy()
                for c_ in ["open","high","low","close"]: df[c_]=pd.to_numeric(df[c_],errors="coerce")
                df["volume"]=pd.to_numeric(df["volume"],errors="coerce").fillna(0); df["amount"]=pd.to_numeric(df["amount"],errors="coerce").fillna(0)
                all_df.append(df)
        except: pass
        cur=ce+timedelta(days=1); fetched+=min(6,(ed-cur).days+1); _tm.sleep(0.3)
        if label:
            _pct=int(fetched/total_days*100)
            if _pct>_last_pct: _last_pct=_pct; print(f"   [数据] {label}: {_pct}% ({cur.strftime('%m-%d')})")
    if not all_df: return pd.DataFrame()
    return pd.concat(all_df,ignore_index=True).drop_duplicates(subset=["time"]).sort_values("time").reset_index(drop=True)
def _dm(mdf,ds):
    if mdf is None or mdf.empty: return pd.DataFrame()
    d=datetime.strptime(ds,"%Y-%m-%d").date(); df=mdf[mdf["time"].dt.date==d].copy()
    if df.empty or len(df[df["time"].dt.hour<=11])<30: return pd.DataFrame()
    return df
def _addi(df):
    if df.empty or len(df)<2: return df
    df=df.copy(); c=df["close"]; d=c.diff(); g=d.clip(lower=0).rolling(14,min_periods=1).mean(); l_=-d.clip(upper=0).rolling(14,min_periods=1).mean()
    df["rsi"]=100-100/(1+g/l_.replace(0,np.nan))
    ma=c.rolling(20,min_periods=1).mean(); sd=c.rolling(20,min_periods=1).std()
    df["bb_up"]=ma+2*sd; df["bb_dn"]=ma-2*sd; df["bb_pct"]=(c-df["bb_dn"])/(df["bb_up"]-df["bb_dn"]).replace(0,np.nan)
    e1=c.ewm(span=12,adjust=False).mean(); e2=c.ewm(span=26,adjust=False).mean()
    df["macd"]=e1-e2; df["macd_signal"]=df["macd"].ewm(span=9,adjust=False).mean(); df["macd_hist"]=(df["macd"]-df["macd_signal"])*2
    df["ema_fast"]=c.ewm(span=3,adjust=False).mean(); df["ema_slow"]=c.ewm(span=6,adjust=False).mean(); df["ema_spread"]=(df["ema_fast"]-df["ema_slow"])/df["ema_slow"].replace(0,np.nan)
    if "amount" in df.columns and df["amount"].notna().sum()>0: df["vwap"]=df["amount"].cumsum()/df["volume"].cumsum()
    else: tp=(df["high"]+df["low"]+df["close"])/3; df["vwap"]=(tp*df["volume"]).cumsum()/df["volume"].cumsum()
    df["vwap"]=df["vwap"].ffill().fillna(c); df["vwap_dev"]=(c-df["vwap"])/df["vwap"].replace(0,np.nan)
    atr=df["high"].sub(df["low"]).abs().rolling(14,min_periods=1).mean(); df["vwap_dev_atr"]=df["vwap_dev"]/(atr/df["close"]).replace(0,np.nan)
    df["date"]=pd.to_datetime(df["time"]).dt.date; dh=df.groupby("date")["high"].transform("max"); dl=df.groupby("date")["low"].transform("min")
    df["day_amplitude"]=(dh-dl)/dl.replace(0,np.nan); df["range_pos"]=(c-dl)/(dh-dl+1e-9)
    df["vol_ma10"]=df["volume"].rolling(10,min_periods=1).mean()
    df["vol_ratio"]=df["volume"]/df["vol_ma10"].replace(0,np.nan); df["mom5"]=c.pct_change(5)
    k=df["high"]-df["low"]+1e-5
    df["upper_shadow"]=(df["high"]-df[["open","close"]].max(axis=1))/k
    df["lower_shadow"]=(df[["open","close"]].min(axis=1)-df["low"])/k
    df["prev_close"]=c.shift(1); df["prev_high"]=df["high"].shift(1); df["today_ret"]=(c-df["prev_close"])/df["prev_close"].replace(0,np.nan)
    return df
class Portfolio:
    def __init__(self): self.cash=IC; self.holdings=IH; self.trades=[]; self.cycles=[]; self.blocked=[]; self.nav_log=[]; self.ib=0; self.is_=0
    def buy(self,ds,ts,price,qty,reason,score=0):
        cost=price*qty; fee=cost*CM
        if self.cash<cost+fee: return False
        # 仓位上限：持仓市值 ≤ 总资产 × 0.60
        total_value = self.cash + self.holdings * price
        new_value = price * qty
        single_pct = new_value / max(total_value, 1)
        if single_pct > 0.60: return False
        self.cash-=cost+fee; self.holdings+=qty; self.ib+=qty
        self.trades.append({"date":ds,"time":ts,"side":"BUY","price":round(price,2),"qty":qty,"fee":round(fee,2)})
        return True
    def sell(self,ds,ts,price,qty,reason,score=0):
        if self.holdings<qty: qty=self.holdings
        if qty<=0: return False
        proceeds=price*qty; fee=proceeds*CM; tax=proceeds*ST
        self.cash+=proceeds-fee-tax; self.holdings-=qty; self.is_+=qty
        self.trades.append({"date":ds,"time":ts,"side":"SELL","price":round(price,2),"qty":qty,"fee":round(fee,2),"tax":round(tax,2)})
        return True
    def cycle(self,bp,sp,q,m,d): net=(sp-bp)*q-bp*q*CM-sp*q*(CM+ST); self.cycles.append({"date":d,"qty":q,"buy_price":round(bp,2),"sell_price":round(sp,2),"net":round(net,2)})
    def eod(self,ds,cp):
        neto=self.ib-self.is_
        if neto>0: p=cp-SL; proceeds=p*neto; self.cash+=proceeds-proceeds*CM-proceeds*ST; self.holdings-=neto
        elif neto<0: p=cp+SL; cost=p*abs(neto); self.cash-=cost+cost*CM; self.holdings+=abs(neto)
        self.ib=0; self.is_=0
        self.nav_log.append({"date":ds,"nav":round(self.cash+self.holdings*cp,2),"cash":round(self.cash,2),"holdings":self.holdings,"close":round(cp,2),"t0_cumulative":round(sum(c["net"] for c in self.cycles),2)})
    def init_nav(self,cp): self.nav_log.append({"date":"init","nav":round(self.cash+self.holdings*cp,2),"cash":round(self.cash,2),"holdings":self.holdings,"close":round(cp,2),"t0_cumulative":0})
def _metrics(nav_log):
    if len(nav_log)<3: return {}
    df=pd.DataFrame(nav_log); fv=df.iloc[0]["nav"]; lv=df.iloc[-1]["nav"]
    tr=float((lv/fv-1)*100); nd=max(len(df)-1,1); ar=float(((lv/fv)**(252/nd)-1)*100)
    df["ret"]=df["nav"].pct_change(); dr=df["ret"].dropna()
    if len(dr)<2: return {"tr":round(tr,2)}
    mdd=float(((df["nav"]/df["nav"].cummax())-1).min()*100)
    sh=float(np.mean(dr-0.03/252)/np.std(dr,ddof=1)*np.sqrt(252)) if np.std(dr,ddof=1)>0 else 0
    return {"tr":round(tr,2),"ar":round(ar,2),"mdd":round(mdd,2),"sh":round(sh,2),"vol":round(float(np.std(dr,ddof=1)*np.sqrt(252)*100),2)}
class Runner:
    def __init__(self,mode="test"): self.mode=mode; self.port=Portfolio(); self._daily=None; self._smin=None; self._imin=None; self._dates=[]
    def load(self):
        print(f"  加载 {self.start}~{self.end}")
        # 个股日线（用于均线计算和状态翻译）
        d="".join(c for c in CODE if c.isdigit())[:6]; s=f"sz{d}"if d.startswith(("0","3"))else f"sh{d}"
        sd=_tcent(s,self.end,600)
        self._daily=sd
        if sd is not None and len(sd)>0: self._dates=sorted(sd[(sd["date"]>=self.start)&(sd["date"]<=self.end)]["date"].tolist())
        if not self._dates: print("  [警告] 无交易日"); return False
        print(f"  {len(self._dates)} 天"); self._smin=_tsmin(CODE,self.start,self.end,"个股")
        if self._smin is None or self._smin.empty: print("  [警告] 分钟空"); return False
        print(f"  [数据] 个股分钟: {len(self._smin)} 行, {self._smin['time'].dt.date.nunique()} 天")
        if self.mode=="test": print("   [数据] 加载大盘分钟..."); self._imin=_tsmin("sh000001",self.start,self.end,"大盘")
        return True
    def _dctx(self,ds):
        ctx=dict.fromkeys(["daily_status","daily_gate","daily_trend_bg","daily_ma5_state","daily_support_name","index_regime","index_circuit_state","index_gate_advice"],"")
        ctx.update({"daily_buy_t_ok":True,"daily_prev_close":0,"daily_prev_high":0,"daily_prev_low":0,"daily_prev_close_real":0,"daily_day_ret":0,"daily_ma5":0,"daily_ma10":0,"daily_ma20":0,"daily_ma60":0,"daily_ma5_slope":0,"daily_ma5_gap":0,"daily_breakdown_risk":False,"daily_hard_breakdown":False,"daily_overheated":False,"daily_pullback_support":False,"daily_near_support":False,"daily_support_level":0,"daily_support_gap":0,"daily_above_ma5":False,"index_pos_factor":1.0,"intraday_alerts":[]})
        dd=self._daily[self._daily["date"]<=ds].copy() if self._daily is not None else None
        if dd is not None and len(dd)>=20:
            try:
                t=dd.iloc[-1]; p=dd.iloc[-2] if len(dd)>=2 else t
                ctx["daily_prev_close"]=float(p.get("close",0)or 0)
                ctx["daily_day_ret"]=(float(t["close"])-ctx["daily_prev_close"])/ctx["daily_prev_close"]if ctx["daily_prev_close"]else 0
                for n in [5,10,20,60]: ctx[f"daily_ma{n}"]=float(t.get(f"ma{n}",0)or 0)
                if ctx["daily_ma5"]>0:
                    ctx["daily_above_ma5"]=float(t["close"])>=ctx["daily_ma5"]
                    ctx["daily_status"]="ok"
                    if ctx["daily_above_ma5"]:
                        ctx["daily_ma5_state"]="above_ma5_trend" if ctx["daily_ma5"]>ctx["daily_ma10"] else "near_ma5_chop"
                        ctx["daily_trend_bg"]="bull"
                    else:
                        ctx["daily_ma5_state"]="below_ma5_weak"
                        ctx["daily_trend_bg"]="bear"
            except: pass
        return ctx
    def run(self,start="2025-06-01",end="2026-07-20"):
        self.start=start; self.end=end
        if not self.load(): return self.port
        import signal_engine as _se; _se.MINUTE_FETCH_STATUS[CODE]="ok"; _se.STOCK_PARAMS.clear()
        _se.STOCK_PARAMS[CODE] = {"buy_confirm_min_score":15,"min_profit_space":0.003,"sell_holding_min_minutes":5,"sell_holding_strict_minutes":15,"sell_score_boost_holding":2,"hard_sell_threshold_cap":200,"hard_buy_threshold_cap":80}
        # Override signal_engine logging to backtest output directory (Test group only)
        # decision_trace.jsonl + shadow_signals.jsonl will contain buy_score, buy_threshold,
        # buy_block_reasons, priority_path for analyzing why 华工科技 missed dip-buy triggers
        if self.mode == "test":
            os.makedirs(_OUT, exist_ok=True)
            _se._trace_path = lambda kind, day=None: os.path.join(_OUT, f"{kind}.jsonl")
            _se._result_trace_path = lambda day=None: os.path.join(_OUT, "signal_outcome.jsonl")
            _se._append_jsonl = lambda p, r: (
                lambda f: (f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n"), f.close())
            )(open(p, "a", encoding="utf-8"))
        _se.PARAMS.update({"min_amplitude":0.002,"rsi_oversold":35,"rsi_overbought":78,"vol_confirm_boost":10,"vol_ratio_confirm":1.2,"macd_strong_threshold":0.2,"macd_strong_boost":25,"min_profit_space":0.008,"buy_confirm_min_score":25,"range_pos_low_threshold":0.3,"range_pos_high_threshold":0.85,"sell_holding_min_minutes":10,"sell_holding_strict_minutes":30,"sell_score_boost_holding":5,"sell_score_boost_eod":8,"sell_momentum_bonus":6})
        eng=_se.SignalEngine(); _start_t=_tm.time(); ie=None
        if self.mode=="test":
            try:
                from index_regime import _IndexRegimeEngine as _IRE
                ie=_IRE
                # import 后立即压制 index_regime 的 logger
                import logging as _lg
                _ir = _lg.getLogger('index_regime')
                _ir.setLevel(_lg.ERROR)
                for _h in _ir.handlers: _ir.removeHandler(_h)
                _ir.handlers.clear()
                _ir.addHandler(_lg.NullHandler())
            except: print("  [警告] index_regime 不可用")
        total=len(self._dates)
        for di,ds in enumerate(self._dates):
            elapsed=_tm.time()-_start_t; eta=elapsed/(di+1)*(total-di-1) if total>0 and di>=0 else 0
            if di%20==0 or di==total-1: print(f"  [{di}/{total}] {ds} | 耗时{elapsed/60:.0f}分 预计剩{eta/60:.0f}分")
            sm=_dm(self._smin,ds)
            if sm.empty: continue
            sm=_addi(sm)
            if sm.empty or len(sm)<=1: continue
            ictx=None
            if ie:
                try: ictx=ie().detect(as_of=ds,mode="morning")[2]
                except: pass
            im=_dm(self._imin,ds)if self._imin is not None else pd.DataFrame()
            if self.mode=="hold" and not self.port.trades:
                self.port.init_nav(float(sm.iloc[0]["close"])); continue
            if di==0 and not self.port.nav_log: self.port.init_nav(float(sm.iloc[0]["close"]))
            h={"name":NAME,"cost":float(sm.iloc[0]["close"]),"qty":self.port.holdings,"t_qty":self.port.holdings,"type":"stock","pre_close":float(sm.iloc[0]["close"])}
            bc,sc=0,0; its=[]
            for i in range(1,len(sm)):
                ss=sm.iloc[:i+1].copy(); alerts=None
                if self.mode=="test" and not im.empty:
                    ct=pd.to_datetime(sm.iloc[i]["time"]); isl=im[im["time"]<=ct].copy()
                    if len(isl)>=30:
                        try:
                            from index_regime_intraday import detect_intraday_alert
                            ia=detect_intraday_alert(isl)
                            if isinstance(ia,dict): alerts=ia.get("alerts",[])
                        except: pass
                dc=self._dctx(ds)
                if self.mode=="test" and ictx: dc["index_regime"]=ictx.get("regime","range")
                if alerts: dc["intraday_alerts"]=alerts
                try: bs,ss_s,sig=eng.evaluate(CODE,NAME,ss,h,daily_ctx=dc)
                except Exception: continue
                if self.mode=="test" and alerts and sig is None and bs>=45:
                    tags=[a.get("tag") for a in alerts if a.get("tag")in("I1","I5")]
                    if tags: self.port.blocked.append({"date":ds,"time":str(sm.iloc[i]["time"]),"reason":",".join(tags),"score":bs})
                if sig is None: continue
                cp=float(sm.iloc[i]["close"]); ct=str(sm.iloc[i]["time"])
                if sig.action in("BUY_LOW","ADD_POS") and bc<MB:
                    p=max(cp,float(sm.iloc[i]["high"]))+SL
                    if self.port.buy(ds,ct,p,FQ,sig.action,sig.score): its.append(("BUY",p,FQ)); bc+=1
                elif sig.action=="SELL_HIGH" and sc<MS:
                    p=min(cp,float(sm.iloc[i]["low"]))-SL
                    if self.port.sell(ds,ct,p,FQ,sig.action,sig.score): its.append(("SELL",p,FQ)); sc+=1
            buys=[t for t in its if t[0]=="BUY"]; sells=[t for t in its if t[0]=="SELL"]
            for j in range(min(len(buys),len(sells))): self.port.cycle(buys[j][1],sells[j][1],FQ,"long",ds)
            self.port.eod(ds,float(sm.iloc[-1]["close"]))
            if bc>0 or sc>0: print(f"    {ds}: {bc}买/{sc}卖 NAV={self.port.nav_log[-1]['nav']:.0f}")
        print(f"  {total}/{total} 天"); return self.port
def report(port,label):
    m=_metrics(port.nav_log); tr=len(port.trades); tc=len(port.cycles)
    buys=sum(1 for t in port.trades if t["side"]=="BUY"); sells=sum(1 for t in port.trades if t["side"]=="SELL")
    print(f"\n{'='*55}\n  {label}\n{'='*55}")
    print(f"  交易:{tr}({buys}买/{sells}卖)  闭环:{tc}  阻断:{len(port.blocked)}")
    if tc>0:
        w=[c for c in port.cycles if c["net"]>0]; l_=[c for c in port.cycles if c["net"]<=0]
        wr=round(len(w)/tc*100,1); pf=round(sum(c["net"] for c in w)/max(abs(sum(c["net"] for c in l_)),0.01),2); avg=round(sum(c["net"] for c in port.cycles)/tc,2)
        print(f"  胜率:{wr}%({len(w)}/{tc})  盈亏比:{pf}  均利:{avg:+.2f}")
    if m: print(f"  总收益:{m['tr']:+.2f}%  年化:{m['ar']:+.2f}%  回撤:{m['mdd']:.2f}%  夏普:{m['sh']}  T0利润:{sum(c['net'] for c in port.cycles):+.0f}元")
    return m
def save(port,od,label):
    os.makedirs(od,exist_ok=True)
    for n,d in [("trades",port.trades),("cycles",port.cycles),("blocked",port.blocked),("nav",port.nav_log)]:
        if d: json.dump(d,open(os.path.join(od,f"{label}_{n}.jsonl"),"w",encoding="utf-8"))
def gen_report(all_m,od,all_p):
    lines=[]
    lines.append("# V1.27 信号引擎 & 大盘联动 回测报告"); lines.append("")
    lines.append("> 标的: "+CODE+" "+NAME)
    lines.append("> 初始状态: 底仓 "+str(IH)+" 股 | 现金 "+str(int(IC))+" 元")
    lines.append("> 交易规则: 固定 "+str(FQ)+" 股/笔 | 每日"+str(MB)+"买/"+str(MS)+"卖 | 佣金万1.5 印花税千0.5")
    lines.append("> 滑点: 买入=最高价+0.01 卖出=最低价-0.01"); lines.append("")
    lines.append("## 1. 三组对比"); lines.append("")
    lines.append("| 组别 | 总收益 | 年化 | 最大回撤 | 夏普 | T0利润 | 阻断 |")
    lines.append("|------|--------|------|----------|------|--------|------|")
    nm={"hold":"Hold(基准)","control":"Control(盲飞)","test":"Test(完全体)"}
    for k in ["hold","control","test"]:
        if k not in all_m or not all_m[k]: lines.append("| "+nm.get(k,k)+" | 数据不足 | - | - | - | - | - |"); continue
        m=all_m[k]; blk=len(all_p[k].blocked)if k in all_p else 0
        r="| "+nm[k]; r+=" | "+"{:+.2f}%".format(m["tr"]); r+=" | "+"{:+.2f}%".format(m["ar"])
        r+=" | "+"{:.2f}%".format(-abs(m["mdd"])); r+=" | "+"{:.2f}".format(m["sh"])
        t0p=sum(c["net"]for c in all_p[k].cycles)if k in all_p else 0; r+=" | "+"{:+.0f}".format(t0p)+"元"; r+=" | "+str(blk)+" |"; lines.append(r)
    lines.append("")
    for k in ["hold","control","test"]:
        if k not in all_m or not all_m[k]: continue
        m=all_m[k]; lines.append("## 2. "+nm[k]); lines.append("")
        lines.append("### 收益指标")
        lines.append("- 总收益率: "+"{:+.2f}%".format(m["tr"]))
        lines.append("- 年化收益率: "+"{:+.2f}%".format(m["ar"]))
        lines.append("- 最大回撤: "+"{:.2f}%".format(-abs(m["mdd"])))
        lines.append("- 夏普比率: "+"{:.2f}".format(m["sh"]))
        lines.append("- 日波动率: "+"{:.2f}%".format(m.get("vol",0))); lines.append("")
        lines.append("### 交易明细")
        lines.append("- 初始现金: "+str(int(IC))+"元 | 初始底仓: "+str(IH)+"股")
        if k in all_p:
            p=all_p[k]
            lines.append("- T0累计利润: "+"{:+.0f}".format(sum(c["net"]for c in p.cycles))+"元")
            buys=sum(1 for t in p.trades if t["side"]=="BUY"); sells=sum(1 for t in p.trades if t["side"]=="SELL")
            lines.append("- 总交易: "+str(len(p.trades))+"笔("+str(buys)+"买/"+str(sells)+"卖)")
            lines.append("- 闭环: "+str(len(p.cycles))+"对")
            if p.cycles:
                w_=[c for c in p.cycles if c["net"]>0]; l_=[c for c in p.cycles if c["net"]<=0]
                wr=round(len(w_)/len(p.cycles)*100,1); pf=round(sum(c["net"]for c in w_)/max(abs(sum(c["net"]for c in l_)),0.01),2)
                lines.append("- 胜率: "+str(wr)+"%("+str(len(w_))+"/"+str(len(p.cycles))+")")
                lines.append("- 盈亏比: "+str(pf))
            lines.append("- 大盘阻断: "+str(len(p.blocked))+"次"); lines.append("")
    # ==================== 信号错过分析 (decision_trace + shadow_signals) ====================
    dt_path=os.path.join(od,"decision_trace.jsonl"); ss_path=os.path.join(od,"shadow_signals.jsonl")
    if os.path.exists(dt_path) or os.path.exists(ss_path):
        lines.append("## 3. 信号错过分析"); lines.append("")
        if os.path.exists(dt_path):
            try:
                _dts=[]; _f=open(dt_path,"r",encoding="utf-8")
                for _l in _f:
                    _l=_l.strip()
                    if _l: _dts.append(json.loads(_l))
                _f.close()
            except: _dts=[]
            if _dts:
                # 近miss买入信号：buy_score >= buy_threshold - 5 但被block
                _buy_near=[r for r in _dts if r.get("decision")!="BUY_LOW" and r.get("buy_score",0)>=r.get("buy_threshold",99)-5]
                _blocked=[r for r in _buy_near if r.get("buy_block_reasons")]
                if _blocked:
                    lines.append(f"### 3a. 买入近miss分析 (buy_score ≥ threshold-5)")
                    lines.append(f"共 {len(_blocked)} 次近miss记录，按日期top-20：")
                    lines.append("")
                    lines.append("| 时间 | buy_score | buy_threshold | 差 | 阻断原因 | priority_path |")
                    lines.append("|------|-----------|---------------|-----|----------|--------------|")
                    for _r in sorted(_blocked,key=lambda x:x.get("buy_score",0)-x.get("buy_threshold",99),reverse=True)[:20]:
                        _gap=_r.get("buy_score",0)-_r.get("buy_threshold",99)
                        _br=",".join(_r.get("buy_block_reasons",[])or[])
                        lines.append(f"| {_r.get('scan_time','')[:16]} | {_r.get('buy_score','')} | {_r.get('buy_threshold','')} | {_gap:+.0f} | {_br} | {_r.get('priority_path','hold')} |")
                    lines.append("")
                # 统计阻断原因出现次数
                _all_reasons={}
                for _r in _dts:
                    for _br in (_r.get("buy_block_reasons") or []):
                        _all_reasons[_br]=_all_reasons.get(_br,0)+1
                if _all_reasons:
                    lines.append("**阻断原因统计（全部决策记录）**：")
                    for _reason,_cnt in sorted(_all_reasons.items(),key=lambda x:-x[1]):
                        lines.append(f"- {_reason}: {_cnt}次")
                    lines.append("")
        if os.path.exists(ss_path):
            try:
                _sss=[]; _f2=open(ss_path,"r",encoding="utf-8")
                for _l2 in _f2:
                    _l2=_l2.strip()
                    if _l2: _sss.append(json.loads(_l2))
                _f2.close()
            except: _sss=[]
            if _sss:
                lines.append("### 3b. 影子信号分析 (距离阈值4分以内)")
                lines.append(f"共 {len(_sss)} 条影子信号（已触发信号的近miss记录）")
                _by_side={}
                for _r in _sss:
                    _side=_r.get("best_signal_type","buy")
                    _by_side.setdefault(_side,[]).append(_r)
                for _side in ["buy","sell"]:
                    _sl=_by_side.get(_side,[])
                    if not _sl: continue
                    _dists=[max(0,_r.get(f"distance_to_{_side}_threshold",4)) for _r in _sl]
                    _avg_dist=sum(_dists)/len(_dists) if _dists else 0
                    _pct_within_2=sum(1 for d in _dists if d<=2)/len(_dists)*100 if _dists else 0
                    _label="买入" if _side=="buy" else "卖出"
                    lines.append(f"- {_label}影子信号: {len(_sl)}次 | 均距阈值 {_avg_dist:.1f}分 | 距≤2分占比 {_pct_within_2:.0f}%")
                lines.append("")
    path=os.path.join(od,"report.md")
    with open(path,"w",encoding="utf-8") as f: f.write("\n".join(lines))
    print("  [报告] MD -> "+path)
def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--start",default="2025-06-01"); ap.add_argument("--end",default="2026-07-20")
    ap.add_argument("--engine",default="all",choices=["hold","control","test","all"]); args=ap.parse_args(); all_m={}; all_p={}
    for mode in ["hold","control","test"]:
        if args.engine not in ("all",mode): continue
        print(f"\n>>> 运行 {mode}")
        r=Runner(mode); port=r.run(args.start,args.end); all_p[mode]=port
        nm={"hold":"Hold 基准","control":"Control(无大盘联动)","test":"Test(双层大盘联动)"}
        m=report(port,nm[mode]); all_m[mode]=m
        if mode!="hold": save(port,_OUT,mode)
    print(f"\n{'='*55}\n  三组对比\n{'='*55}")
    print(f"  {'组别':<10} {'总收益':>8} {'年化':>8} {'最大回撤':>8} {'夏普':>6} {'T0利润':>8} {'阻断':>6}")
    for k in ["hold","control","test"]:
        if k not in all_m: continue; m=all_m[k]
        if not m: print(f"  {k:<10} [数据不足]"); continue
        t0p=sum(c["net"]for c in all_p[k].cycles)if k in all_p else 0; blk=len(all_p[k].blocked)if k=="test"and k in all_p else 0
        print(f"  {k:<10} {m['tr']:>+7.1f}% {m['ar']:>+7.1f}% {m['mdd']:>7.1f}% {m['sh']:>5.1f} {t0p:>+7.0f} {blk:>6}")
    print(f"\n  输出: {_OUT}"); gen_report(all_m,_OUT,all_p)
if __name__=="__main__":
    main()
