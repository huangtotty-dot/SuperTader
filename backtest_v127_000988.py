# -*- coding: utf-8 -*-
"""
华工科技 V1.27 信号引擎回测
用法: python backtest_v127_000988.py --engine all
"""
import argparse, json, os, sys, time as _time_mod, urllib.request
from datetime import datetime, timedelta
import numpy as np, pandas as pd

TUSHARE_TOKEN = "9d15f39266cbbf8a1e5efa1525d7a4d4d1dbc62ec8cbce167d642def"
CODE = "000988"; NAME = "华工科技"
FIXED_QTY = 200; MAX_BUY = 3; MAX_SELL = 3; BASE = 1000
COMM = 0.00015; STAMP = 0.0005; SLIP = 0.01
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
                recs.append({"date":str(row[0])[:10],"open":float(row[1]),"close":float(row[2]),
                    "high":float(row[3]),"low":float(row[4]),"volume":float(row[5]),"amount":a})
            except: continue
        if not recs: return None
        df=pd.DataFrame(recs).drop_duplicates(subset="date",keep="last").sort_values("date").reset_index(drop=True)
        for c in ["open","high","low","close"]: df[c]=pd.to_numeric(df[c],errors="coerce")
        for n in [5,10,20,60]: df[f"ma{n}"]=df["close"].rolling(n).mean()
        return df
    except: return None

def _tsmin(code,start,end):
    d="".join(c for c in code if c.isdigit())[:6]
    tc=f"{d}.{'SZ' if d.startswith(('0','3')) else 'SH'}"
    pro=_ts(); all_df=[]; cur=datetime.strptime(start,"%Y-%m-%d"); ed=datetime.strptime(end,"%Y-%m-%d")
    while cur<=ed:
        ce=min(cur+timedelta(days=6),ed)
        s=cur.strftime("%Y-%m-%d 09:00:00"); e=ce.strftime("%Y-%m-%d 15:30:00")
        try:
            df=pro.stk_mins(ts_code=tc,freq="1min",start_date=s,end_date=e)
            if df is not None and not df.empty:
                df=df.rename(columns={"trade_time":"time","vol":"volume"})
                df["time"]=pd.to_datetime(df["time"])
                h,m2=df["time"].dt.hour,df["time"].dt.minute
                df=df[((h==9)&(m2>=30))|(h==10)|((h==11)&(m2<=30))|((h==13)&(m2>=0))|(h==14)|((h==15)&(m2==0))].copy()
                for c_ in ["open","high","low","close"]: df[c_]=pd.to_numeric(df[c_],errors="coerce")
                df["volume"]=pd.to_numeric(df["volume"],errors="coerce").fillna(0)
                df["amount"]=pd.to_numeric(df["amount"],errors="coerce").fillna(0)
                all_df.append(df)
        except: pass
        cur=ce+timedelta(days=1); _time_mod.sleep(0.3)
    if not all_df: return pd.DataFrame()
    return pd.concat(all_df,ignore_index=True).drop_duplicates(subset=["time"]).sort_values("time").reset_index(drop=True)

def _dm(mdf,ds):
    if mdf is None or mdf.empty: return pd.DataFrame()
    d=datetime.strptime(ds,"%Y-%m-%d").date()
    df=mdf[mdf["time"].dt.date==d].copy()
    if df.empty or len(df[df["time"].dt.hour<=11])<30: return pd.DataFrame()
    return df

def _addi(df):
    if df.empty or len(df)<2: return df
    df=df.copy(); c=df["close"]
    d=c.diff(); g=d.clip(lower=0).rolling(14,min_periods=1).mean(); l_=-d.clip(upper=0).rolling(14,min_periods=1).mean()
    df["rsi"]=100-100/(1+g/l_.replace(0,np.nan))
    ma=c.rolling(20,min_periods=1).mean(); sd=c.rolling(20,min_periods=1).std()
    df["bb_up"]=ma+2*sd; df["bb_dn"]=ma-2*sd; df["bb_pct"]=(c-df["bb_dn"])/(df["bb_up"]-df["bb_dn"]).replace(0,np.nan)
    e1=c.ewm(span=12,adjust=False).mean(); e2=c.ewm(span=26,adjust=False).mean()
    df["macd"]=e1-e2; df["macd_signal"]=df["macd"].ewm(span=9,adjust=False).mean(); df["macd_hist"]=(df["macd"]-df["macd_signal"])*2
    df["ema_fast"]=c.ewm(span=3,adjust=False).mean(); df["ema_slow"]=c.ewm(span=6,adjust=False).mean()
    df["ema_spread"]=(df["ema_fast"]-df["ema_slow"])/df["ema_slow"].replace(0,np.nan)
    if "amount" in df.columns and df["amount"].notna().sum()>0: df["vwap"]=df["amount"].cumsum()/df["volume"].cumsum()
    else: tp=(df["high"]+df["low"]+df["close"])/3; df["vwap"]=(tp*df["volume"]).cumsum()/df["volume"].cumsum()
    df["vwap"]=df["vwap"].ffill().fillna(c); df["vwap_dev"]=(c-df["vwap"])/df["vwap"].replace(0,np.nan)
    atr=df["high"].sub(df["low"]).abs().rolling(14,min_periods=1).mean()
    df["vwap_dev_atr"]=df["vwap_dev"]/(atr/df["close"]).replace(0,np.nan)
    df["date"]=pd.to_datetime(df["time"]).dt.date
    dh=df.groupby("date")["high"].transform("max"); dl=df.groupby("date")["low"].transform("min")
    df["day_amplitude"]=(dh-dl)/dl.replace(0,np.nan); df["range_pos"]=(c-dl)/(dh-dl+1e-9)
    df["vol_ma10"]=df["volume"].rolling(10,min_periods=1).mean()
    df["vol_ratio"]=df["volume"]/df["vol_ma10"].replace(0,np.nan); df["mom5"]=c.pct_change(5)
    k=df["high"]-df["low"]+1e-5
    df["upper_shadow"]=(df["high"]-df[["open","close"]].max(axis=1))/k
    df["lower_shadow"]=(df[["open","close"]].min(axis=1)-df["low"])/k
    df["prev_close"]=c.shift(1); df["prev_high"]=df["high"].shift(1)
    df["today_ret"]=(c-df["prev_close"])/df["prev_close"].replace(0,np.nan)
    return df

class Log:
    def __init__(self): self.trades=[]; self.cycles=[]; self.blocked=[]
    def buy(self,d,t,p,q,r): self.trades.append({"date":d,"time":t,"side":"BUY","price":p,"qty":q,"reason":r})
    def sell(self,d,t,p,q,r): self.trades.append({"date":d,"time":t,"side":"SELL","price":p,"qty":q,"reason":r})
    def block(self,d,t,r,s): self.blocked.append({"date":d,"time":t,"reason":r,"score":s})
    def cycle(self,bp,sp,q,m,d):
        fb=bp*q*COMM; fs=sp*q*(COMM+STAMP); n=(sp-bp)*q-fb-fs
        self.cycles.append({"date":d,"mode":m,"qty":q,"buy_price":round(bp,2),"sell_price":round(sp,2),"net":round(n,2)})
    def summary(self):
        tn=sum(c["net"] for c in self.cycles); w=[c for c in self.cycles if c["net"]>0]; l_=[c for c in self.cycles if c["net"]<=0]
        tw=sum(c["net"] for c in w); tl=abs(sum(c["net"] for c in l_))
        return {"trades":len(self.trades),"cycles":len(self.cycles),"win":len(w),"lose":len(l_),
            "win_rate":round(len(w)/max(1,len(self.cycles))*100,2),"total_net":round(tn,2),
            "profit_factor":round(tw/max(tl,0.01),2),"avg_net":round(tn/max(1,len(self.cycles)),2),"blocked":len(self.blocked)}

class Runner:
    def __init__(self,mode="test"):
        self.mode=mode; self.log=Log(); self._daily=None; self._smin=None; self._imin=None; self._dates=[]
    def load(self):
        print(f"\n=== 加载 {self.start}~{self.end} ===")
        self._daily=_tcent("sh000001",self.end,800)
        if self._daily is not None: print(f"  大盘日线: {len(self._daily)} 条")
        d="".join(c for c in CODE if c.isdigit())[:6]; s=f"sz{d}" if d.startswith(("0","3")) else f"sh{d}"
        sd=_tcent(s,self.end,600)
        if sd is not None and len(sd)>0: self._dates=sorted(sd[(sd["date"]>=self.start)&(sd["date"]<=self.end)]["date"].tolist())
        print(f"  交易日: {len(self._dates)} 天\n  加载分钟...")
        self._smin=_tsmin(CODE,self.start,self.end)
        if self._smin is None or self._smin.empty: print("[警告] 分钟空"); return False
        print(f"  {len(self._smin)} 行, {self._smin['time'].dt.date.nunique()} 天")
        if self.mode=="test":
            self._imin=_tsmin("sh000001",self.start,self.end)
            if self._imin is not None: print(f"  大盘分钟: {len(self._imin)} 行")
        return True
    def _dctx(self,ds):
        ctx=dict.fromkeys(["daily_status","daily_gate","daily_trend_bg","daily_ma5_state","daily_support_name",
            "index_regime","index_circuit_state","index_gate_advice"],"")
        ctx.update({"daily_buy_t_ok":True,"daily_prev_close":0,"daily_prev_high":0,"daily_prev_low":0,
            "daily_prev_close_real":0,"daily_day_ret":0,"daily_ma5":0,"daily_ma10":0,"daily_ma20":0,"daily_ma60":0,
            "daily_ma5_slope":0,"daily_ma5_gap":0,"daily_breakdown_risk":False,"daily_hard_breakdown":False,
            "daily_overheated":False,"daily_pullback_support":False,"daily_near_support":False,
            "daily_support_level":0,"daily_support_gap":0,"daily_above_ma5":False,
            "index_pos_factor":1.0,"intraday_alerts":[]})
        dd=self._daily[self._daily["date"]<=ds].copy() if self._daily is not None else None
        if dd is not None and len(dd)>=20:
            try:
                t=dd.iloc[-1]; p=dd.iloc[-2] if len(dd)>=2 else t
                ctx["daily_prev_close"]=float(p.get("close",0)or 0)
                ctx["daily_day_ret"]=(float(t["close"])-ctx["daily_prev_close"])/ctx["daily_prev_close"] if ctx["daily_prev_close"] else 0
                for n in [5,10,20,60]: ctx[f"daily_ma{n}"]=float(t.get(f"ma{n}",0)or 0)
                if ctx["daily_ma5"]>0: ctx["daily_above_ma5"]=float(t["close"])>=ctx["daily_ma5"]
            except: pass
        return ctx

    def run(self,start="2025-06-01",end="2026-07-20"):
        self.start=start; self.end=end
        if not self.load(): return self.log
        # 直接 import signal_engine（自带独立模式回退）
        import signal_engine as _se
        _se.MINUTE_FETCH_STATUS[CODE] = "ok"
        _se.STOCK_PARAMS.clear()
        _se.PARAMS.update({"min_amplitude":0.002,"rsi_oversold":35,"rsi_overbought":78,"vol_confirm_boost":10,
            "vol_ratio_confirm":1.2,"macd_strong_threshold":0.2,"macd_strong_boost":25,
            "min_profit_space":0.008,"buy_confirm_min_score":25,"range_pos_low_threshold":0.3,
            "range_pos_high_threshold":0.85,"sell_holding_min_minutes":10,"sell_holding_strict_minutes":30,
            "sell_score_boost_holding":5,"sell_score_boost_eod":8,"sell_momentum_bonus":6,
            "cooldown_minutes":5,"repeat_signal_gap_minutes":5,"repeat_signal_price_move":0.003,
            "repeat_signal_score_boost":10,"sell_repeat_block_minutes":10,"post_sell_rebuild_minutes":10,
            "post_sell_rebuild_price_gap":0.005,"post_sell_rebuild_score_gap":8,"post_sell_rebuild_min_seconds":120,
            "post_sell_rebuild_buy_threshold_penalty":15,"post_sell_rebuild_weak_gate_discount":3,
            "post_sell_rebuild_relax_gap":4,"stand_down_score_gap":8,"stand_down_flat_range_gap":0.005,
            "market_state_threshold_bias":3,"etf_stand_down_gap":0.003,"daily_support_buy_boost":5,
            "daily_trend_buy_boost":3,"daily_breakdown_buy_penalty":15,"daily_breakdown_sell_boost":8,
            "daily_downtrend_buy_penalty":10,"daily_overheat_buy_penalty":5,"daily_overheat_sell_boost":8,
            "daily_overheat_buy_threshold_penalty":5,"daily_support_buy_threshold_relief":3,
            "daily_risk_buy_threshold_penalty":10,"buy_confirm_min_factors":3,"buy_confirm_min_seconds":30,
            "buy_rebound_min_score_gap":5,"sell_confirm_min_factors":3,"sell_confirm_min_seconds":30,
            "buy_starvation_days":5,"buy_starvation_relax_factors":1,"buy_starvation_relax_gap":3,
            "buy_starvation_relax_seconds":10,"max_buy_times_per_stock":5,"max_sell_times_per_stock":5,
            "max_t_cycles_per_stock":8,"stock_min_trade_unit":100,"etf_min_trade_unit":100,
            "rsi_period":14,"bb_period":20,"bb_std":2,"ema_fast_period":3,"ema_slow_period":6,
            "trend_today_ret_threshold":0.03,"rsi_15m_oversold":35,"min_15min_bars":3})
        eng = _se.SignalEngine()

        ie=None
        if self.mode=="test":
            try:
                from index_regime import _IndexRegimeEngine as _IRE
                ie = _IRE
            except: print("  [警告] index_regime 不可用")

        total=len(self._dates)
        for di,ds in enumerate(self._dates):
            if di%20==0: print(f"  进度: {di}/{total} ({ds})")
            sm=_dm(self._smin,ds)
            if sm.empty: continue
            sm=_addi(sm)
            if sm.empty or len(sm)<30: continue
            ictx=None
            if ie:
                try: ictx=ie().detect(as_of=ds,mode="morning")[2]
                except: pass
            im=_dm(self._imin,ds) if self._imin is not None else pd.DataFrame()
            if self.mode=="hold":
                if self.log.cycles: continue
                self.log.buy(ds,str(sm.iloc[0]["time"]),float(sm.iloc[0]["close"]),BASE,"init"); continue
            cost=float(sm.iloc[0]["close"])
            h={"name":NAME,"cost":cost,"qty":BASE,"t_qty":BASE,"type":"stock","pre_close":cost}
            bc,sc=0,0; its=[]
            for i in range(1,len(sm)):
                ss=sm.iloc[:i+1].copy()
                alerts=None
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
                    tags=[a.get("tag") for a in alerts if a.get("tag") in ("I1","I5")]
                    if tags: self.log.block(ds,str(sm.iloc[i]["time"]),f"大盘{','.join(tags)}",bs)
                if sig is None: continue
                cp=float(sm.iloc[i]["close"]); ct=str(sm.iloc[i]["time"])
                if sig.action in ("BUY_LOW","ADD_POS") and bc<MAX_BUY:
                    p=max(cp,float(sm.iloc[i]["high"]))+SLIP
                    self.log.buy(ds,ct,round(p,2),FIXED_QTY,sig.action); its.append(("BUY",p,FIXED_QTY)); bc+=1
                elif sig.action=="SELL_HIGH" and sc<MAX_SELL:
                    p=min(cp,float(sm.iloc[i]["low"]))-SLIP
                    self.log.sell(ds,ct,round(p,2),FIXED_QTY,sig.action); its.append(("SELL",p,FIXED_QTY)); sc+=1
            buys=[t for t in its if t[0]=="BUY"]; sells=[t for t in its if t[0]=="SELL"]
            for j in range(min(len(buys),len(sells))): self.log.cycle(buys[j][1],sells[j][1],FIXED_QTY,"long",ds)
            if bc>0 or sc>0: print(f"    {ds}: {bc}买/{sc}卖 → {min(bc,sc)}闭环")
        print(f"  {total}/{total} 天"); return self.log

def report(log,label):
    s=log.summary()
    print(f"\n{'='*55}\n  {label}\n{'='*55}")
    print(f"  交易:{s['trades']}  闭环:{s['cycles']}  胜率:{s['win_rate']}%({s['win']}/{s['cycles']})")
    print(f"  净利润:{s['total_net']:+.2f}  盈亏比:{s['profit_factor']}  均利:{s['avg_net']:.2f}  阻断:{s['blocked']}")
    return s

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--start",default="2025-06-01"); ap.add_argument("--end",default="2026-07-20")
    ap.add_argument("--engine",default="all",choices=["hold","control","test","all"])
    args=ap.parse_args(); sm={}
    for mode in ["hold","control","test"]:
        if args.engine not in ("all",mode): continue
        r=Runner(mode); log=r.run(args.start,args.end)
        nm={"hold":"Hold基准","control":"Control:无大盘联动","test":"Test:双层大盘联动"}
        sm[mode]=report(log,nm[mode])
        if mode!="hold":
            os.makedirs(_OUT,exist_ok=True)
            for n,d in [("trades",log.trades),("cycles",log.cycles),("blocked",log.blocked)]:
                if d: json.dump(d,open(os.path.join(_OUT,f"{mode}_{n}.jsonl"),"w",encoding="utf-8"))
    print(f"\n{'='*55}\n  对比\n{'='*55}")
    print(f"  {'组别':<10} {'净利润':>10} {'胜率':>8} {'盈亏比':>8} {'阻断':>6}")
    for k in ["hold","control","test"]:
        if k in sm: s=sm[k]; print(f"  {k:<10} {s['total_net']:>+10.0f} {s['win_rate']:>7.1f}% {s['profit_factor']:>7.2f} {s['blocked']:>6}")
    print(f"\n  输出: {_OUT}")

if __name__=="__main__":
    main()
