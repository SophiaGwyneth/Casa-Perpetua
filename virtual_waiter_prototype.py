"""
virtual_waiter_prototype.py - Casa Perpetua table-top kiosk (Lab 6.1, Option 1: web dashboard)

Run:   python virtual_waiter_prototype.py      ->  http://localhost:5000
Needs: ollama serve  +  ollama pull qwen2.5     (the kiosk still works for browsing and the +/- cart without it)

Layout (per the lab handout)
  Left   - conversation + voice stream, category ribbon, text input
  Right  - live order cart synced to SQLite (notes as badges, +/- overrides)
  Bottom - FSM progress + Review / Confirm order, receipt modal on completion

Kiosk flow
  1. Welcome screen asks "Table for how many?"  - tap +/- or press the mic and say it ("four", "party of six", "just me").
  2. Start ordering -> the waiter greets out loud ("... May I take your order?") and opens the mic once for the answer.
  3. Voice modes (arrow next to the mic): Tap to speak, or Automatic mic (hands-free between replies).
  The table number comes from the URL (http://localhost:5000/?table=7, default 4) - one kiosk per table.
"""
import os
import random
import threading
import webbrowser

import requests
from flask import Flask, Response, jsonify, request, send_from_directory

import db_tools
from fsm_agent import CasaPerpetuaAgent, OLLAMA_CHAT_URL, FINAL_STATES

app = Flask(__name__)
agent = CasaPerpetuaAgent()

HTML_KIOSK = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Casa Perpetua - Table kiosk</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@500;600;700&family=Jost:wght@400;500;600&family=Playfair+Display:ital,wght@0,400;0,500;1,400;1,500&family=Share+Tech+Mono&display=swap" rel="stylesheet">
<style>
/* ---------- Design tokens: pulled from the sitting-area photo ---------- */
:root{
  --linen:#F5EDDD;        /* plastered wall */
  --linen-lift:#FCF8EF;   /* napkins, cards */
  --oat:#E4D5B9;          /* channel-tufted banquette */
  --oat-deep:#D3BF9A;
  --walnut:#46352A;       /* table tops, bottom bar */
  --ink:#3A2D22;
  --muted:#7C6D58;
  --brass:#C9A25B;        /* sconces */
  --brass-deep:#8F6B2A;
  --glow:rgba(255,214,140,.55);
  --olive:#5F6B45;        /* cushions, plants */
  --olive-soft:#E3E6D1;
  --hair:#E3D6BD;
  --danger:#8C3B2E;
  --serif:'Cormorant Garamond','Palatino Linotype',Georgia,serif;
  --sans:'Jost','Segoe UI',system-ui,-apple-system,sans-serif;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{
  font-family:var(--sans);color:var(--ink);font-size:16px;line-height:1.45;
  background:
    radial-gradient(120% 55% at 50% -12%,var(--glow),transparent 62%),   /* ceiling cove light */
    linear-gradient(180deg,#F8F1E2,var(--linen) 40%,#EFE4CE);
  overflow:hidden;
}
button{font-family:inherit;font-size:inherit;color:inherit;cursor:pointer;border:none;background:none}
input{font-family:inherit;font-size:inherit;color:inherit}
:focus-visible{outline:3px solid var(--brass);outline-offset:2px;border-radius:8px}
svg{display:block}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}

.app{display:grid;grid-template-rows:auto minmax(0,1fr) auto;grid-template-columns:minmax(0,1fr);height:100vh;height:100dvh}

/* ---------- Header ---------- */
.top{display:flex;align-items:center;justify-content:space-between;padding:12px 28px;border-bottom:1px solid var(--brass);position:relative}
.top::after{content:"";position:absolute;left:8%;right:8%;bottom:-1px;height:2px;background:linear-gradient(90deg,transparent,var(--brass),transparent);box-shadow:0 0 16px 3px var(--glow)}
.brand{display:flex;align-items:center;gap:12px}
.brand h1{font-family:var(--serif);font-weight:600;font-size:1.75rem;line-height:1;letter-spacing:.01em}
.brand p{font-family:var(--serif);font-style:italic;color:var(--muted);font-size:1.02rem}
.table-chip{font-weight:500;border:1px solid var(--olive);color:var(--olive);padding:6px 16px;border-radius:999px;background:var(--linen-lift)}

/* ---------- Stage ---------- */
.stage{display:flex;min-height:0;min-width:0}
.talk{flex:1;display:flex;flex-direction:column;min-width:0}

.ribbon{display:flex;gap:8px;padding:12px 20px;overflow-x:auto;scrollbar-width:thin;scrollbar-color:var(--oat-deep) transparent;border-bottom:1px solid var(--hair)}
.chip{flex:none;display:flex;align-items:center;gap:7px;padding:8px 16px;border-radius:999px;border:1px solid var(--oat-deep);background:var(--linen-lift);font-weight:500;min-height:44px;transition:background .2s,color .2s,border-color .2s}
.chip:hover{border-color:var(--olive);background:var(--olive-soft)}
.chip[aria-pressed="true"]{background:var(--olive);border-color:var(--olive);color:var(--linen-lift)}
.chip .em{font-size:1.1rem}
.ribbon[hidden]{display:none}
/* looping category ribbon: auto-scrolls, but can be dragged, wheeled, arrowed or paused */
.ribbonbar{display:flex;align-items:center;gap:6px;padding:0 14px;border-bottom:1px solid var(--hair)}
.ribbonbar #ribbon{flex:1;min-width:0;border-bottom:0;padding:12px 6px;scrollbar-width:none;cursor:grab;overscroll-behavior:contain}
.ribbonbar #ribbon::-webkit-scrollbar{display:none}
#ribbon.dragging{cursor:grabbing;user-select:none}
#ribbon .chip{user-select:none;-webkit-user-select:none}
.rb-btn{flex:none;width:40px;height:40px;border-radius:50%;border:1px solid var(--oat-deep);background:var(--linen-lift);display:grid;place-items:center;font-size:1.25rem;line-height:1;color:inherit;transition:background .2s,border-color .2s}
.rb-btn:hover{border-color:var(--olive);background:var(--olive-soft)}

.ribbon.sub{padding:8px 20px 10px;background:var(--olive-soft);border-bottom:1px solid var(--hair)}
.ribbon.sub .chip{min-height:38px;padding:5px 14px;font-size:.95rem}
.temp{display:flex;gap:6px;margin:8px 0 2px}
.tbtn{flex:1;min-height:34px;padding:4px 0;border:1px solid var(--oat-deep);border-radius:999px;background:var(--linen-lift);font-size:.85rem;font-weight:500}
.tbtn[aria-pressed="true"]{background:var(--olive);border-color:var(--olive);color:var(--linen-lift)}
.sub-h{grid-column:1/-1;margin:6px 0 -4px;font-weight:600;font-size:.95rem;color:var(--olive)}

.stream{flex:1;overflow-y:auto;padding:20px 24px;display:flex;flex-direction:column;gap:16px;scroll-behavior:smooth}
.msg{max-width:min(92%,880px);padding:12px 16px;line-height:1.5}
.msg .who{font-size:.8rem;color:var(--muted);margin-bottom:2px}
.msg.wide{width:min(100%,900px)}
.msg.waiter{align-self:flex-start;background:var(--linen-lift);border:1px solid var(--hair);border-radius:18px 18px 18px 4px;box-shadow:0 6px 18px -12px rgba(70,53,42,.5)}
.msg.guest{align-self:flex-end;background:var(--olive);color:var(--linen-lift);border-radius:18px 18px 4px 18px}
.msg.guest .who{color:#D8DDC4}
.typing{display:inline-flex;gap:5px;padding:6px 2px}
.typing i{width:7px;height:7px;border-radius:50%;background:var(--brass);animation:blink 1.1s infinite ease-in-out}
.typing i:nth-child(2){animation-delay:.18s}.typing i:nth-child(3){animation-delay:.36s}
@keyframes blink{0%,80%,100%{opacity:.25;transform:translateY(0)}40%{opacity:1;transform:translateY(-3px)}}

/* Menu cards: photo cropped into an arch, like the niche in the dining room */
.menu{display:grid;grid-template-columns:repeat(auto-fill,minmax(168px,1fr));gap:14px;margin-top:12px}
.dish{display:flex;flex-direction:column;background:#FFFDF8;border:1px solid var(--hair);border-radius:18px;padding:10px 10px 12px}
.arch{position:relative;height:132px;border-radius:999px 999px 12px 12px;overflow:hidden;background:linear-gradient(180deg,var(--oat),var(--linen))}
.arch .tile{position:absolute;inset:0;display:grid;place-items:center;font-size:2.8rem}
.arch img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.arch.photo .tile,.thumb.photo .tile,.bd-plate.photo .tile{display:none}   /* transparent photos (e.g. Water) must not show the emoji behind them */
.dish h3{font-family:var(--serif);font-size:1.25rem;font-weight:600;line-height:1.15;margin:10px 4px 2px}
.dish .price{font-weight:600;color:var(--brass-deep);margin:0 4px}
.dish .meta{font-size:.82rem;color:var(--muted);margin:2px 4px 8px;min-height:1.2em}
.dish .low{color:var(--danger)}
.add{margin-top:auto;display:flex;align-items:center;justify-content:center;gap:6px;min-height:44px;border-radius:999px;background:var(--olive);color:var(--linen-lift);font-weight:500;transition:background .2s}
.add:hover{background:#4E5937}

/* Menu board: striped photo panel + big title + dotted price list (same look as the printed pasta menu) */
.board{display:grid;grid-template-columns:minmax(0,46fr) minmax(0,54fr);margin-top:12px;border-radius:16px;overflow:hidden;
  background:#FDF3E0;border:1px solid var(--hair);color:#5B3A22;font-family:'Playfair Display',var(--serif),Georgia,serif}
.bd-photos{display:grid;grid-template-columns:1fr 1fr;grid-auto-rows:min-content;align-content:start;gap:10px;padding:14px 12px;
  background:repeating-linear-gradient(90deg,#F4E3C4 0 14px,#FDF3E0 14px 28px)}
.bd-card{display:flex;flex-direction:column;align-items:center;gap:6px;padding:10px 6px 8px;background:#FFFBF2;border:2px solid #FBE2A6;
  border-radius:20px;color:inherit;font:inherit;text-align:center;min-height:132px;transition:box-shadow .15s,border-color .15s}
.bd-card:hover{border-color:var(--brass)}
.bd-card[aria-pressed="true"]{border-color:var(--olive);box-shadow:0 0 0 3px var(--olive-soft)}
.bd-card.out{opacity:.5}
.bd-plate{position:relative;flex:none;width:84px;height:84px;border-radius:50%;overflow:hidden;background:radial-gradient(circle at 50% 38%,#FFFFFF,#EBDDC2);box-shadow:0 2px 6px rgba(91,58,34,.22)}
.bd-plate .tile{position:absolute;inset:0;display:grid;place-items:center;font-size:2.3rem}
.bd-plate img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.bd-plate.photo{width:116px;height:116px;margin:-8px 0 -8px;background:none;box-shadow:none;overflow:visible;border-radius:0}
.bd-plate.photo img{object-fit:contain}
.bd-name{font-size:.95rem;line-height:1.12}
.bd-info{display:flex;flex-direction:column;padding:16px 24px 12px;min-width:0}
.bd-title{font-size:clamp(2.6rem,6vw,4.2rem);font-weight:500;line-height:1;text-align:center;letter-spacing:-.01em;margin-top:4px}
.bd-pill{align-self:center;margin:8px 0 12px;padding:0 32px;border:1.5px solid #5B3A22;border-radius:999px;background:#F1DFC0;font-style:italic;font-size:1.9rem;line-height:1.35}
.bd-row{display:flex;align-items:baseline;gap:10px;width:100%;padding:10px 6px;border-radius:10px;color:inherit;font:inherit;text-align:left;font-size:1.02rem}
.bd-row:hover{background:rgba(201,162,91,.16)}
.bd-row[aria-pressed="true"]{background:var(--olive-soft);font-weight:500}
.bd-row .lead{flex:1;min-width:16px;border-bottom:2px dotted #5B3A22;opacity:.75;transform:translateY(-4px)}
.bd-row small{font-size:.72rem;opacity:.65;margin-left:6px;font-style:italic}
.bd-row.out{opacity:.5}
.bd-pager{display:flex;align-items:center;justify-content:center;gap:12px;margin-top:auto;padding-top:8px;font-size:.9rem}
.bd-pager button{width:36px;height:36px;border-radius:50%;border:1px solid var(--oat-deep);background:var(--linen-lift);font-size:1.1rem;line-height:1}
.bd-pager button:disabled{opacity:.35}
.bd-pager[hidden]{display:none}
.dish.picker{flex-direction:row;flex-wrap:wrap;align-items:center;gap:4px 16px;margin-top:10px}
.dish.picker h3{margin:0}
.dish.picker .meta{margin:0;min-height:0}
.dish.picker .temp{margin:0;flex:0 1 auto}
.dish.picker .tbtn{flex:none;padding:4px 14px}
.dish.picker .add{margin-top:0;margin-left:auto;padding:0 22px}
.bd-hint{margin-top:10px;font-size:.9rem;color:var(--muted)}
@media (max-width:760px){.board{grid-template-columns:1fr}.bd-info{order:-1}}
.add:disabled{background:var(--oat);color:var(--muted);cursor:not-allowed}

.composer{display:flex;gap:10px;align-items:center;padding:14px 20px;border-top:1px solid var(--hair);background:rgba(252,248,239,.6)}
.voice{display:flex;align-items:center;gap:8px;min-height:48px;padding:0 16px 0 12px;border-radius:999px;border:1.5px solid var(--brass);background:var(--linen-lift);font-weight:500;white-space:nowrap}
.voice[aria-pressed="true"]{background:var(--olive);border-color:var(--olive);color:var(--linen-lift)}
.voice.listening svg{animation:ring 1.4s infinite}
.voice:disabled{opacity:.5;cursor:not-allowed}
.voicegrp{position:relative;display:flex;flex:none}
.voicegrp .voice{border-radius:999px 0 0 999px;border-right:none}
.caret{display:grid;place-items:center;width:40px;min-height:48px;border:1.5px solid var(--brass);border-left:1px solid var(--oat-deep);border-radius:0 999px 999px 0;background:var(--linen-lift)}
.caret:hover{background:var(--olive-soft)}
.caret svg{transition:transform .2s}.caret[aria-expanded="true"] svg{transform:rotate(180deg)}
.voicegrp.on .caret{background:var(--olive);border-color:var(--olive);color:var(--linen-lift)}
.vmenu{position:absolute;left:0;bottom:calc(100% + 8px);z-index:10;min-width:250px;padding:6px;background:var(--linen-lift);border:1px solid var(--hair);border-radius:16px;box-shadow:0 14px 34px -12px rgba(70,53,42,.55)}
.vmenu[hidden]{display:none}
.vmenu button{display:flex;align-items:flex-start;gap:10px;width:100%;text-align:left;padding:10px 12px;border-radius:12px;min-height:48px}
.vmenu button:hover{background:var(--olive-soft)}
.vmenu .dot{flex:none;width:16px;height:16px;margin-top:3px;border-radius:50%;border:2px solid var(--oat-deep)}
.vmenu button[aria-checked="true"] .dot{border-color:var(--olive);background:radial-gradient(circle,var(--olive) 45%,transparent 50%)}
.vmenu b{display:block;font-weight:600}.vmenu small{color:var(--muted)}
@keyframes ring{0%{filter:drop-shadow(0 0 0 rgba(255,255,255,.9))}60%{filter:drop-shadow(0 0 7px rgba(255,255,255,.9))}100%{filter:drop-shadow(0 0 0 rgba(255,255,255,0))}}
.composer input{flex:1;min-width:0;min-height:48px;padding:0 20px;border-radius:999px;border:1.5px solid var(--oat-deep);background:#FFFDF8}
.composer input::placeholder{color:#9A8B73}
.send{min-height:48px;padding:0 24px;border-radius:999px;background:var(--olive);color:var(--linen-lift);font-weight:500;display:flex;align-items:center;gap:8px}
.send:hover{background:#4E5937}

/* ---------- Cart: channel-tufted banquette ---------- */
.cart{flex:0 0 clamp(350px,34vw,440px);display:flex;flex-direction:column;min-height:0;padding:20px 20px 18px;
  background-color:var(--oat);
  background-image:repeating-linear-gradient(90deg,rgba(84,60,32,.10) 0,rgba(84,60,32,0) 7px,rgba(255,250,235,.22) 30px,rgba(84,60,32,0) 52px);
  border-left:1px solid var(--oat-deep)}
.cart-head{display:flex;align-items:baseline;justify-content:space-between;margin-bottom:12px}
.cart-head h2{font-family:var(--serif);font-weight:700;font-size:1.75rem;line-height:1}
.cart-head span{color:var(--muted);font-size:.9rem}
.lines{flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:10px;padding-right:2px}
.empty{margin:auto;text-align:center;color:var(--muted);max-width:240px}
.empty p{margin-top:8px}
.line{display:grid;grid-template-columns:54px 1fr auto;gap:6px 12px;align-items:start;background:var(--linen-lift);border:1px solid var(--hair);border-radius:16px;padding:10px 12px}
.thumb{grid-row:span 2;width:54px;height:64px;border-radius:27px 27px 8px 8px;overflow:hidden;position:relative;background:linear-gradient(180deg,var(--oat),var(--linen))}
.thumb .tile{position:absolute;inset:0;display:grid;place-items:center;font-size:1.5rem}
.thumb img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.line h4{font-family:var(--serif);font-size:1.15rem;font-weight:600;line-height:1.15}
.line .each{font-size:.82rem;color:var(--muted)}
.line .total{font-weight:600;white-space:nowrap}
.badges{display:flex;flex-wrap:wrap;gap:5px;margin-top:4px}
.badge{font-size:.78rem;padding:2px 10px;border-radius:999px;background:var(--olive-soft);color:#3F4A2B;border:1px solid #C9CFAE}
.actions{grid-column:2 / 4;display:flex;align-items:center;gap:14px;margin-top:2px}
.qty{display:flex;align-items:center;border:1px solid var(--oat-deep);border-radius:999px;background:#FFFDF8}
.qty button{width:40px;height:40px;display:grid;place-items:center;border-radius:50%}
.qty button:hover{background:var(--olive-soft)}
.qty output{min-width:26px;text-align:center;font-weight:600}
.link{font-size:.88rem;color:var(--muted);text-decoration:underline;text-underline-offset:3px;min-height:40px;padding:0 2px}
.link:hover{color:var(--ink)}
.noteform{grid-column:1 / 4;display:flex;gap:8px;margin-top:4px}
.noteform input{flex:1;min-width:0;min-height:44px;padding:0 14px;border-radius:999px;border:1.5px solid var(--brass);background:#fff}
.noteform button{min-height:44px;padding:0 16px;border-radius:999px;background:var(--olive);color:var(--linen-lift);font-weight:500}
.noteform button.ghost{background:none;color:var(--muted);border:1px solid var(--oat-deep)}

.totals{margin-top:14px;padding:14px 16px;background:var(--linen-lift);border:1px solid var(--hair);border-radius:16px}
.totals div{display:flex;justify-content:space-between;color:var(--muted)}
.totals .grand{margin-top:8px;padding-top:8px;border-top:1px dashed var(--oat-deep);color:var(--ink);align-items:baseline}
.totals .grand span:first-child{font-family:var(--serif);font-size:1.3rem;font-weight:600}
.totals .grand span:last-child{font-family:var(--serif);font-size:1.9rem;font-weight:700;color:var(--brass-deep)}

/* ---------- Mode of payment (shown in the cart while the order is in REVIEW) ---------- */
.pay{margin-top:12px;padding:12px 14px 14px;background:var(--linen-lift);border:1px solid var(--hair);border-radius:16px}
.pay[hidden]{display:none}
.pay h3{font-family:var(--serif);font-size:1.3rem;font-weight:700;line-height:1.1;margin-bottom:8px}
.payopts{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.popt{display:flex;align-items:center;gap:8px;min-height:48px;padding:6px 12px;border:1.5px solid var(--oat-deep);border-radius:14px;background:#FFFDF8;font-weight:500;text-align:left;transition:background .2s,border-color .2s}
.popt:hover{border-color:var(--olive);background:var(--olive-soft)}
.popt[aria-checked="true"]{background:var(--olive);border-color:var(--olive);color:var(--linen-lift)}
.popt .em{font-size:1.25rem;line-height:1}
.paynote{margin-top:8px;font-size:.88rem;color:var(--muted);min-height:1.2em}
.paynote.need{color:var(--danger);font-weight:500}

/* ---------- Bottom bar: FSM + checkout (walnut tabletop) ---------- */
.bar{display:flex;align-items:center;justify-content:space-between;gap:20px;flex-wrap:wrap;padding:14px 28px;background:var(--walnut);color:var(--linen);border-top:3px solid var(--brass)}
.steps{display:flex;align-items:center;list-style:none;gap:0}
.steps li{display:flex;align-items:center;gap:8px;color:#BFAE94;font-weight:500}
.steps li+li::before{content:"";width:34px;height:2px;background:#6A5645;margin:0 10px}
.steps li.done+li::before,.steps li.now::before{background:var(--brass)}
.steps i{width:14px;height:14px;border-radius:50%;border:2px solid #8A7660;flex:none}
.steps li.done{color:var(--linen)}.steps li.done i{background:var(--brass);border-color:var(--brass)}
.steps li.now{color:#fff}.steps li.now i{background:var(--brass);border-color:#FFE7B0;box-shadow:0 0 0 4px rgba(201,162,91,.3),0 0 14px 3px var(--glow)}
.cancelled-note{color:#E9B8AA;font-weight:500}
.cta{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.btn{min-height:50px;padding:0 24px;border-radius:999px;font-weight:600;display:flex;align-items:center;gap:8px;transition:transform .15s,background .2s}
.btn.primary{background:var(--brass);color:#2F2210;font-size:1.05rem;padding:0 34px;box-shadow:0 0 22px -2px var(--glow)}
.btn.primary:hover{background:#D7B06A}
.btn.primary:disabled{background:#6A5645;color:#B5A38B;box-shadow:none;cursor:not-allowed}
.btn.outline{border:1.5px solid var(--brass);color:var(--linen)}
.btn.outline:hover{background:rgba(201,162,91,.18)}
.btn.quiet{color:#CDBBA0;font-weight:500;padding:0 12px;text-decoration:underline;text-underline-offset:4px}
.btn.quiet:hover{color:#fff}
.btn.quiet.armed{color:#F3B7A8}

/* ---------- Welcome: Aeris greets the table (the card is baked into aeris.png; this layer sits inside it) ---------- */
.welcome{position:fixed;inset:0;z-index:20;overflow:hidden;background:#2B1D14;container-type:size}
.welcome[hidden]{display:none}
/* Spare space (only on very tall / narrow windows) is filled with a blurred copy of the artwork */
.welcome::before{content:"";position:absolute;inset:-40px;background:url(/aeris.png) center/cover no-repeat;filter:blur(26px) brightness(.8)}
/* Fits any browser window: the artwork covers the whole window (centred, edges cropped) as long as Aeris and the card stay fully visible;
   on very tall / narrow windows it shrinks just enough to keep them in view. 1255 x 783 is the safe area of the 1920 x 1080 artwork. */
.wstage{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);
  width:min(max(100cqw,calc(100cqh * 16 / 9)),calc(100cqw * 1.499),calc(100cqh * 2.403));
  aspect-ratio:16/9;container-type:inline-size;background:url(/aeris.png) center/100% 100% no-repeat}
.welcome.noimg .wstage{background:linear-gradient(180deg,#F8F0E0,#EADCC0)}
.welcome.noimg .wstage::before{content:"";position:absolute;left:18.6%;top:23%;width:63.2%;height:54.4%;border-radius:1.6cqw;
  background:rgba(214,176,120,.85);border:.2cqw dotted #6B4A3A}
.wbox{position:absolute;left:41.8%;top:26.8%;width:37.2%;height:45.2%;display:flex;flex-direction:column;justify-content:space-between;gap:.8cqw;
  color:var(--ink);font-family:var(--sans)}
.whead{display:flex;justify-content:space-between;align-items:center}
.wname{font-size:max(10px,.85cqw);letter-spacing:.16em;text-transform:uppercase;font-weight:600;color:#5A4232}
.wtable{font-size:max(11px,1cqw);font-weight:500;padding:.3cqw 1cqw;border-radius:999px;background:#FFFDF8;border:1px solid var(--brass);color:var(--ink)}
.bubble{position:relative;display:flex;align-items:center;gap:1.2cqw;padding:1.3cqw 1.8cqw;background:#FFFDF8;border-radius:1.8cqw;box-shadow:0 .4cqw 1.4cqw rgba(70,53,42,.22)}
.bubble::before{content:"";position:absolute;left:-.85cqw;top:50%;transform:translateY(-50%);border:.9cqw solid transparent;border-left:0;border-right-color:#FFFDF8}
.welcome.lit .bubble{animation:bpop .55s cubic-bezier(.2,.9,.3,1.15) both}
@keyframes bpop{from{opacity:0;transform:translateY(.8cqw) scale(.94)}to{opacity:1;transform:none}}
.wave{display:inline-block;flex:none;font-size:3cqw;line-height:1;transform-origin:70% 80%}
.welcome.lit .wave{animation:wave 2.2s ease-in-out .5s 2}
.welcome.talking .wave{animation:wave 1.8s ease-in-out infinite}
@keyframes wave{0%,60%,100%{transform:rotate(0)}10%{transform:rotate(16deg)}20%{transform:rotate(-8deg)}30%{transform:rotate(16deg)}40%{transform:rotate(-4deg)}50%{transform:rotate(10deg)}}
.hello{font-family:var(--serif);font-weight:600;font-size:2.1cqw;line-height:1.18}
.hello span{display:inline-block;opacity:0;transform:translateY(.4em)}
.welcome.lit .hello span{animation:wordin .45s ease forwards;animation-delay:calc(var(--i) * .11s + .35s)}
@keyframes wordin{to{opacity:1;transform:none}}
.eq{display:none;align-items:flex-end;gap:.25cqw;height:1.6cqw;margin-left:auto;flex:none}
.welcome.talking .eq{display:flex}
.eq i{display:block;width:.28cqw;min-width:3px;height:100%;border-radius:2px;background:var(--olive);transform-origin:bottom;animation:eq .9s ease-in-out infinite}
.eq i:nth-child(2){animation-delay:.15s}.eq i:nth-child(3){animation-delay:.3s}.eq i:nth-child(4){animation-delay:.45s}
@keyframes eq{0%,100%{transform:scaleY(.25)}50%{transform:scaleY(1)}}
.wrow{display:flex;align-items:center;gap:1.1cqw}
.stepper{display:inline-flex;align-items:center;gap:.3cqw;border:max(1.5px,.1cqw) solid var(--oat-deep);border-radius:999px;background:#FFFDF8;padding:.25cqw}
.stepper button{width:max(34px,2.9cqw);height:max(34px,2.9cqw);border-radius:50%;display:grid;place-items:center}
.stepper button:hover{background:var(--olive-soft)}
.stepper button:disabled{opacity:.3;cursor:not-allowed}
.stepper output{min-width:3.6cqw;text-align:center;font-family:var(--serif);font-size:2.2cqw;font-weight:700}
.wunit{font-size:max(12px,1.05cqw);color:#5A4232}
.talk-btn{margin-left:auto;display:inline-flex;align-items:center;justify-content:center;gap:.6cqw;min-height:max(38px,3cqw);padding:0 1.4cqw;border-radius:999px;
  border:1px solid var(--brass);background:#FFFDF8;font-weight:500;font-size:max(12px,1.1cqw);color:var(--ink);transition:background .2s,color .2s}
.talk-btn:hover{background:var(--olive-soft)}
.talk-btn[aria-pressed="true"]{background:var(--olive);border-color:var(--olive);color:var(--linen-lift)}
.talk-btn.listening svg{animation:ring 1.4s infinite}
.talk-btn.idle{animation:nudge 2.4s ease-in-out infinite}
@keyframes nudge{0%,100%{box-shadow:0 0 0 0 rgba(255,205,120,.7)}50%{box-shadow:0 0 0 8px rgba(255,205,120,0)}}
.heard{min-height:1.3em;font-family:var(--serif);font-style:italic;font-weight:600;font-size:max(13px,1.3cqw);color:#4A3628;text-align:center}
.start{width:100%;min-height:max(44px,3.1cqw);border-radius:999px;background:var(--olive);color:var(--linen-lift);font-weight:600;font-size:max(15px,1.3cqw);box-shadow:0 0 0 max(2px,.15cqw) var(--brass)}
.start:hover{background:#4E5937}
.welcome .closed{align-self:center;padding:.3cqw 1cqw;border-radius:999px;background:#FFFDF8;font-size:max(12px,1.05cqw);color:var(--danger);font-weight:600;text-align:center}
@media (prefers-reduced-motion:reduce){
  .welcome .bubble,.welcome .wave,.welcome .hello span,.talk-btn.idle{animation:none!important}
  .hello span{opacity:1;transform:none}
}

/* ---------- Receipt ---------- */
.modal{position:fixed;inset:0;z-index:30;display:grid;place-items:center;padding:16px;background:rgba(58,43,28,.55);backdrop-filter:blur(3px)}
.modal[hidden]{display:none}
.paper{width:min(400px,100%);max-height:94vh;overflow:auto;background:#FFFDF7;border-radius:6px 6px 0 0;padding:26px 26px 22px;box-shadow:0 30px 60px -20px rgba(0,0,0,.55);animation:rise .45s ease-out}
@keyframes rise{from{transform:translateY(24px);opacity:0}to{transform:none;opacity:1}}
.paper header{text-align:center;padding-bottom:14px;border-bottom:1px dashed var(--oat-deep)}
.paper header h2{font-family:var(--serif);font-size:1.8rem;font-weight:700;line-height:1.1;margin-top:4px}
.paper .info{display:flex;justify-content:space-between;color:var(--muted);font-size:.9rem;margin-top:6px}
.paper ul{list-style:none;padding:14px 0;border-bottom:1px dashed var(--oat-deep)}
.paper li{display:flex;justify-content:space-between;gap:12px;padding:3px 0}
.paper li small{display:block;color:var(--muted)}
.paper .sum{padding-top:10px}
.paper .sum div{display:flex;justify-content:space-between;color:var(--muted)}
.paper .sum .grand{color:var(--ink);font-family:var(--serif);font-size:1.6rem;font-weight:700;margin-top:4px}
.paper .thanks{text-align:center;font-family:var(--serif);font-style:italic;font-size:1.2rem;color:var(--olive);margin:14px 0 16px}
.paper .row{display:flex;gap:10px}
.paper .row button{flex:1;min-height:48px;border-radius:999px;font-weight:600}
.paper .row .p{background:var(--olive);color:var(--linen-lift)}
.paper .row .s{border:1.5px solid var(--oat-deep)}

.toast{position:fixed;left:50%;bottom:92px;transform:translate(-50%,20px);z-index:40;background:var(--walnut);color:var(--linen);padding:12px 22px;border-radius:999px;opacity:0;pointer-events:none;transition:opacity .25s,transform .25s;box-shadow:0 10px 30px -8px rgba(0,0,0,.5);max-width:90vw}
.toast.show{opacity:1;transform:translate(-50%,0)}

@media (max-width:960px){
  body{overflow:auto}
  .app{height:auto;min-height:100dvh}
  .stage{flex-direction:column}
  .talk{min-height:70vh}
  .cart{flex:none;border-left:none;border-top:1px solid var(--oat-deep)}
  .lines{max-height:50vh}
  .top{padding:12px 16px}
  .bar{padding:12px 16px;position:sticky;bottom:0;z-index:5}
  .steps li span{display:none}
  .steps li.now span{display:inline}
}
@media print{
  body>*:not(#receipt){display:none!important}
  #receipt{position:static;background:none;backdrop-filter:none;padding:0}
  .paper{box-shadow:none;max-height:none}
  .paper .row{display:none}
}
/* ---------- Receiptify-style receipt: crumpled-paper texture, sprig logo and barcode are drawn in CSS/SVG (no image file needed) ---------- */
.rc-wrap{width:min(380px,92vw);display:flex;flex-direction:column;gap:12px}
.paper.rcp{width:100%;max-height:calc(94vh - 66px);overflow:auto;padding:0;background:#F7F1E4 url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='460' height='460'%3E%3Cfilter id='c' x='0' y='0' width='100%25' height='100%25'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.011 .014' numOctaves='4' seed='9' stitchTiles='stitch'/%3E%3CfeDiffuseLighting lighting-color='%23fffbf1' surfaceScale='3.4'%3E%3CfeDistantLight azimuth='235' elevation='68'/%3E%3C/feDiffuseLighting%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url%28%23c%29'/%3E%3C/svg%3E") 0 0/460px 460px;border-radius:0;box-shadow:0 30px 50px -18px rgba(0,0,0,.6);
  -webkit-print-color-adjust:exact;print-color-adjust:exact;scrollbar-width:none}
.paper.rcp::-webkit-scrollbar{display:none}
.rc-top{padding:24px 0 6px;text-align:center}
.rc-logo{width:36px;height:36px;margin:0 auto 10px}
.rc-brand{font-family:'Playfair Display',var(--serif);font-weight:400;font-size:1.75rem;line-height:1.1;letter-spacing:.005em;color:#2f2f2f}
.rc-mid{padding:2px 11% 6px;
  font-family:'Share Tech Mono','Courier New',ui-monospace,monospace;font-size:13px;line-height:1.35;color:#3b3b3b;text-transform:uppercase}
.rc-bot{padding:20px 11% 18px;text-align:center}
.rc-bar{width:60%;height:30px;margin:0 auto;background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 46' preserveAspectRatio='none'%3E%3Crect x='0' width='2' height='46' fill='%233f4040'/%3E%3Crect x='3' width='4' height='46' fill='%233f4040'/%3E%3Crect x='10' width='2' height='46' fill='%233f4040'/%3E%3Crect x='15' width='1' height='46' fill='%233f4040'/%3E%3Crect x='17' width='1' height='46' fill='%233f4040'/%3E%3Crect x='21' width='1' height='46' fill='%233f4040'/%3E%3Crect x='24' width='2' height='46' fill='%233f4040'/%3E%3Crect x='27' width='1' height='46' fill='%233f4040'/%3E%3Crect x='31' width='2' height='46' fill='%233f4040'/%3E%3Crect x='34' width='1' height='46' fill='%233f4040'/%3E%3Crect x='36' width='1' height='46' fill='%233f4040'/%3E%3Crect x='38' width='2' height='46' fill='%233f4040'/%3E%3Crect x='41' width='4' height='46' fill='%233f4040'/%3E%3Crect x='47' width='2' height='46' fill='%233f4040'/%3E%3Crect x='52' width='2' height='46' fill='%233f4040'/%3E%3Crect x='55' width='1' height='46' fill='%233f4040'/%3E%3Crect x='59' width='1' height='46' fill='%233f4040'/%3E%3Crect x='63' width='4' height='46' fill='%233f4040'/%3E%3Crect x='69' width='3' height='46' fill='%233f4040'/%3E%3Crect x='73' width='1' height='46' fill='%233f4040'/%3E%3Crect x='77' width='3' height='46' fill='%233f4040'/%3E%3Crect x='81' width='2' height='46' fill='%233f4040'/%3E%3Crect x='84' width='2' height='46' fill='%233f4040'/%3E%3Crect x='87' width='4' height='46' fill='%233f4040'/%3E%3Crect x='94' width='3' height='46' fill='%233f4040'/%3E%3C/svg%3E") center/100% 100% no-repeat}
.sr-only{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.rc-sub{text-align:center;letter-spacing:.14em;font-size:12px;margin:2px 0 12px}
.rc-meta{margin-bottom:6px;overflow-wrap:anywhere}
.rc-line{border:0;border-top:1px dashed #5a5a5a;margin:6px 0}
.rc-head,.rc-item{display:grid;grid-template-columns:2.3em 1fr auto;column-gap:.6em}
.rc-head{padding:1px 0}
.rc-item{padding:2px 0}
.rc-item .amt,.rc-head .amt{text-align:right}
.rc-item .nm{overflow-wrap:anywhere}
.rc-item .note{grid-column:2/4;font-size:11.5px;opacity:.75}
.rc-row{display:flex;justify-content:space-between;gap:.6em;padding:1px 0}
.rc-thanks{text-align:center;margin:12px 0 4px;letter-spacing:.06em}
.rc-code{margin-top:9px;text-align:center;font-family:'Share Tech Mono','Courier New',ui-monospace,monospace;font-size:12px;letter-spacing:.12em;color:#3b3b3b}
.rc-wrap .row{display:flex;gap:10px}
.rc-wrap .row button{flex:1;min-height:48px;border-radius:999px;font-weight:600}
.rc-wrap .row .p{background:var(--olive);color:var(--linen-lift)}
.rc-wrap .row .s{background:var(--linen-lift);border:1.5px solid var(--oat-deep)}
@media print{
  .paper.rcp{max-height:none;overflow:visible;box-shadow:none;width:80mm}
  .rc-wrap{width:80mm}
  .rc-wrap .row{display:none}
}
@media (prefers-reduced-motion:reduce){
  *{animation:none!important;transition:none!important;scroll-behavior:auto!important}
}
</style>
</head>
<body>
<div class="app">
  <header class="top">
    <div class="brand">
      <svg width="40" height="40" viewBox="0 0 40 40" aria-hidden="true">
        <path d="M20 38C19 29 20 18 27 4" fill="none" stroke="#8F6B2A" stroke-width="1.6" stroke-linecap="round"/>
        <path d="M19.6 31C13 31 10 27 10 22c6 0 9.200 3.800 9.600 9z" fill="#5F6B45"/>
        <path d="M20.600 25C27 25 30 21 30 16c-6 0-9.200 3.800-9.400 9z" fill="#5F6B45"/>
        <path d="M19.400 20C14 19 12 15.500 12.200 11.500c5 .6 7.600 3.600 7.200 8.500z" fill="#7C8860"/>
        <path d="M23.800 14C29 13 30.500 9.500 30 6c-4.500.8-6.600 3.400-6.200 8z" fill="#7C8860"/>
      </svg>
      <div><h1>Casa Perpetua</h1><p>Good food, good mood.</p></div>
    </div>
    <div class="table-chip" id="tableChip">Table -</div>
  </header>

  <main class="stage">
    <section class="talk" aria-label="Conversation with your waiter">
      <div class="ribbonbar" id="ribbonBar">
        <button class="rb-btn" id="rbPrev" type="button" aria-label="Scroll categories left">&#8249;</button>
        <nav class="ribbon" id="ribbon" aria-label="Menu categories"></nav>
        <button class="rb-btn" id="rbNext" type="button" aria-label="Scroll categories right">&#8250;</button>
      </div>
      <nav class="ribbon sub" id="subRibbon" aria-label="Beverage types" hidden></nav>
      <div class="stream" id="stream" role="log" aria-live="polite"></div>
      <form class="composer" id="composer" autocomplete="off">
        <div class="voicegrp" id="voiceGrp">
          <button type="button" class="voice" id="voiceBtn" aria-pressed="false" title="Talk to your waiter">
            <span id="voiceIcon"></span><span id="voiceLabel">Tap to speak</span>
          </button>
          <button type="button" class="caret" id="voiceCaret" aria-haspopup="menu" aria-expanded="false" aria-label="Voice mode"><span id="caretIcon"></span></button>
          <div class="vmenu" id="voiceMenu" role="menu" hidden>
            <button type="button" role="menuitemradio" data-mode="tap" aria-checked="true"><span class="dot"></span><span><b>Tap to speak</b><small>Press the mic, say your order, then it sends</small></span></button>
            <button type="button" role="menuitemradio" data-mode="auto" aria-checked="false"><span class="dot"></span><span><b>Automatic mic</b><small>Hands-free: always listening between replies</small></span></button>
          </div>
        </div>
        <label class="sr" for="userInput">Message your waiter</label>
        <input id="userInput" type="text" placeholder="Try: add 2 cheeseburgers, no onions" />
        <button class="send" type="submit"><span id="sendIcon"></span>Send</button>
      </form>
    </section>

    <aside class="cart" aria-label="Your order">
      <div class="cart-head"><h2>Your order</h2><span id="cartCount">Updates as you order</span></div>
      <div class="lines" id="lines"></div>
      <section class="pay" id="payBox" aria-label="Mode of payment" hidden>
        <h3>Mode of payment</h3>
        <div class="payopts" id="payOpts" role="radiogroup" aria-label="How would you like to pay?"></div>
        <p class="paynote" id="payNote" aria-live="polite"></p>
      </section>
      <div class="totals">
        <div><span>Subtotal</span><span id="subVal">PHP 0.00</span></div>
        <div><span>Service fee (10%)</span><span id="feeVal">PHP 0.00</span></div>
        <div class="grand"><span>Grand total</span><span id="grandVal">PHP 0.00</span></div>
      </div>
    </aside>
  </main>

  <footer class="bar">
    <ol class="steps" id="steps" aria-label="Order progress"></ol>
    <div class="cta" id="cta"></div>
  </footer>
</div>

<!-- Welcome / table capture (STATE_GREETING) -->
<div class="welcome" id="welcome">
  <div class="wstage">
    <div class="wbox">
      <div class="whead">
        <span class="wname">Aeris · Your waiter</span>
        <span class="wtable" id="wTable">Table 4</span>
      </div>
      <div class="bubble" role="status" aria-live="polite">
        <span class="wave" aria-hidden="true">👋</span>
        <p class="hello" id="helloText">Hi, I'm Aeris! Welcome to Casa Perpetua. Table for how many?</p>
        <span class="eq" aria-hidden="true"><i></i><i></i><i></i><i></i></span>
      </div>
      <div class="wrow">
        <div class="stepper" role="group" aria-label="Number of guests">
          <button type="button" id="tMinus" aria-label="Fewer guests"></button>
          <output id="tVal" aria-live="polite">2</output>
          <button type="button" id="tPlus" aria-label="More guests"></button>
        </div>
        <span class="wunit" id="tUnit">guests</span>
        <button type="button" class="talk-btn idle" id="welcomeMic" aria-pressed="false"><span id="wMicIcon"></span><span id="wMicLabel">Tap to talk with Aeris</span></button>
      </div>
      <p class="heard" id="heard" aria-live="polite"></p>
      <button class="start" id="startBtn" type="button">Start ordering</button>
      <p class="closed" id="closedMsg" hidden></p>
    </div>
  </div>
</div>

<!-- Digital receipt -->
<div class="modal" id="receipt" hidden>
  <div class="rc-wrap" role="dialog" aria-modal="true" aria-labelledby="rcTitle">
    <div class="paper rcp" id="rcPaper">
      <div class="rc-top">
        <svg class="rc-logo" viewBox="0 0 40 40" aria-hidden="true">
          <path d="M20 38C19 29 20 18 27 4" fill="none" stroke="#8F6B2A" stroke-width="1.6" stroke-linecap="round"/>
          <path d="M19.6 31C13 31 10 27 10 22c6 0 9.200 3.800 9.600 9z" fill="#5F6B45"/>
          <path d="M20.600 25C27 25 30 21 30 16c-6 0-9.200 3.800-9.400 9z" fill="#5F6B45"/>
          <path d="M19.400 20C14 19 12 15.500 12.200 11.500c5 .6 7.600 3.600 7.200 8.500z" fill="#7C8860"/>
          <path d="M23.800 14C29 13 30.500 9.500 30 6c-4.500.8-6.600 3.400-6.200 8z" fill="#7C8860"/>
        </svg>
        <h2 class="rc-brand" id="rcTitle">Casa Perpetua</h2>
      </div>
      <div class="rc-mid">
        <p class="rc-sub">Good food, good mood</p>
        <div class="rc-meta"><div id="rcNo"></div><div id="rcTable"></div><div id="rcTime"></div></div>
        <hr class="rc-line">
        <div class="rc-head"><span>Qty</span><span>Item</span><span class="amt">Amt</span></div>
        <hr class="rc-line">
        <div id="rcItems"></div>
        <hr class="rc-line">
        <div class="rc-row"><span>Item count:</span><span id="rcCount"></span></div>
        <div class="rc-row"><span>Subtotal:</span><span id="rcSub"></span></div>
        <div class="rc-row"><span>Service fee (10%):</span><span id="rcFee"></span></div>
        <hr class="rc-line">
        <div class="rc-row"><span>Total:</span><span id="rcTotal"></span></div>
        <hr class="rc-line">
        <div class="rc-row"><span>Payment:</span><span id="rcPay"></span></div>
        <div class="rc-row"><span>Party of:</span><span id="rcParty"></span></div>
        <div class="rc-row"><span>Status:</span><span>Sent to kitchen</span></div>
        <p class="rc-thanks">Thank you for visiting!</p>
      </div>
      <div class="rc-bot"><div class="rc-bar" aria-hidden="true"></div><div class="rc-code" id="rcCode"></div></div>
    </div>
    <div class="row"><button class="s" id="rcPrint" type="button">Print receipt</button><button class="p" id="rcNew" type="button">New order</button></div>
  </div>
</div>

<div class="toast" id="toast" role="status"></div>

<script>
/* ===================== helpers ===================== */
const $ = (s) => document.querySelector(s);
const ICON = {Pasta:'🍝',Pastries:'🥐',Beverages:'🥤',Desserts:'🍰',Snacks:'🍿',Dairy:'🥛',Burgers:'🍔',Chicken:'🍗',Filipino:'🍲',Italian:'🍕',Japanese:'🍣',Korean:'🍚',Spanish:'🥘',Thai:'🍜',Chinese:'🥡'};
const CATS = Object.keys(ICON);
const SUBS = [['Coffee','☕'],['Non-Coffee','🥛'],['Refreshers','🍹']];   // Beverages sub-categories
const SUBICON = Object.fromEntries(SUBS);
const PAY_ICON = {'Cash':'💵','GCash':'📱','Maya':'💚','Credit/Debit Card':'💳'};   // mode-of-payment options (names come from the server)
const SVG = {
  mic:'<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg>',
  send:'<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M13 6l6 6-6 6"/></svg>',
  plus:'<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>',
  minus:'<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M5 12h14"/></svg>',
  chev:'<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>',
  leaf:'<svg width="56" height="56" viewBox="0 0 40 40"><path d="M20 38C19 29 20 18 27 4" fill="none" stroke="#B9A57F" stroke-width="1.4" stroke-linecap="round"/><path d="M19.6 31C13 31 10 27 10 22c6 0 9.200 3.800 9.600 9z" fill="#C9CFAE"/><path d="M20.600 25C27 25 30 21 30 16c-6 0-9.200 3.800-9.400 9z" fill="#C9CFAE"/></svg>'
};
function el(tag, props, ...kids){
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})){
    if (v == null || v === false) continue;
    if (k === 'class') n.className = v;
    else if (k === 'text') n.textContent = v;
    else if (k === 'html') n.innerHTML = v;            // static icons only, never user text
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? '' : v);
  }
  kids.flat().forEach(c => { if (c != null && c !== false) n.append(c.nodeType ? c : document.createTextNode(c)); });
  return n;
}
const money = (n) => 'PHP ' + Number(n || 0).toFixed(2);
let toastTimer;
function toast(msg){
  const t = $('#toast'); t.textContent = msg; t.classList.add('show');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove('show'), 3200);
}
async function api(path, body, method){
  const res = await fetch(path, {
    method: method || (body ? 'POST' : 'GET'),
    headers: {'Content-Type':'application/json'},
    body: body ? JSON.stringify(body) : undefined
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 410) { sessionLost(); throw new Error('session_expired'); }
  return data;
}

/* ===================== state ===================== */
const MAX_PARTY = 20;
const urlTable = parseInt(new URLSearchParams(location.search).get('table'), 10);
const S = {sid:null, table:(urlTable >= 1 && urlTable <= 99 ? urlTable : 1 + Math.floor(Math.random() * 20)), party:2, fsm:'GREETING',
           order:{items:[],subtotal:0,service_fee_10_pct:0,grand_total:0,item_count:0},
           cartKey:'', editing:null, busy:false, typed:false, voice:false, tap:false, mode:'tap', speaking:false, cat:null, sub:null, cancelArmed:false,
           starting:false, partyMisses:0, sttBroken:false, said:[]};
const STEPS = [['GREETING','Greeting'],['ORDERING','Ordering'],['REVIEW','Review'],['CONFIRMED','Confirmed']];

/* ===================== welcome / session ===================== */
const NUMW = {one:1,two:2,three:3,four:4,five:5,six:6,seven:7,eight:8,nine:9,ten:10,eleven:11,twelve:12,thirteen:13,
              fourteen:14,fifteen:15,sixteen:16,seventeen:17,eighteen:18,nineteen:19,twenty:20};
/* "4", "four", "table for four", "party of six", "just me", "a couple"  ->  number, or null when it is not a party size */
function parsePartySize(text){
  const t = String(text || '').toLowerCase();
  if (/\b(just me|only me|myself|by myself|on my own|alone|solo)\b/.test(t)) return 1;
  if (/\bcouple\b/.test(t)) return 2;
  const digit = t.match(/\b(\d{1,2})\b/);
  if (digit){ const n = +digit[1]; return n >= 1 && n <= MAX_PARTY ? n : null; }
  for (const [w, n] of Object.entries(NUMW)) if (new RegExp('\\b' + w + '\\b').test(t)) return n;
  if (/\b(?:for|of)\s+(?:to|too|tu)\b/.test(t) || /^\s*(?:to|too)\W*$/.test(t)) return 2;   // the recogniser hears "two" as "to"
  if (/^\s*for\W*$/.test(t)) return 4;                                                         // ... and "four" as "for"
  return null;
}
function renderChip(){ $('#tableChip').textContent = `Table ${S.table} · Party of ${S.party}`; $('#wTable').textContent = `Table ${S.table}`; }
function renderParty(){
  $('#tVal').textContent = S.party;
  $('#tUnit').textContent = S.party === 1 ? 'guest' : 'guests';
  $('#tMinus').disabled = S.party <= 1; $('#tPlus').disabled = S.party >= MAX_PARTY;
  renderChip();
}
/* What the host says out loud when the guests arrive (spoken only - the arch shows "Table for how many?"). */
const HELLO = "Hi, I'm Aeris! Welcome to Casa Perpetua. Table for how many?";
/* The greeting bubble: the words fade in one by one (Aeris herself stays still). */
function renderHello(){
  const p = $('#helloText'); p.replaceChildren();
  HELLO.split(' ').forEach((word, i) => { const sp = el('span', {text: word}); sp.style.setProperty('--i', i); p.append(sp, ' '); });
}
/* Every new party is seated at a table picked by the server (random, never one with an open order).
   Open the kiosk as /?table=7 to pin it to one table instead. */
async function assignTable(){
  if (urlTable >= 1 && urlTable <= 99) return;
  try {
    const d = await (await fetch('/api/assign_table')).json();
    if (d.ok && !S.starting && !$('#welcome').hidden){ S.table = d.table_no; renderChip(); }
  } catch (e) {}                                          // offline: keep the table already shown
}
/* If aeris.png is missing, fall back to a plain card instead of a blank screen. */
{ const probe = new Image(); probe.onerror = () => $('#welcome').classList.add('noimg'); probe.src = '/aeris.png'; }
function showWelcome(closedMsg){
  const w = $('#welcome');
  renderHello();
  stopListening(); cancelSpeech(); S.tap = false;
  S.party = 2; S.partyMisses = 0; $('#heard').textContent = ''; renderParty(); assignTable();
  $('#closedMsg').hidden = !closedMsg; $('#closedMsg').textContent = closedMsg || '';
  w.hidden = false; w.classList.remove('lit');
  requestAnimationFrame(() => requestAnimationFrame(() => w.classList.add('lit')));     // light the arch
  $('#startBtn').focus();
  setVoiceUI();
  if (S.voice && !closedMsg) setTimeout(hostAsk, 600);     // automatic-mic mode: the kiosk asks the next guests out loud
}
function sessionLost(){ S.sid = null; showWelcome('This session ended. Tell me how many guests to start a new order.'); }

/* One listening window: used after the kiosk asks a question, whatever the voice mode is. */
function listenOnce(){ S.tap = true; setVoiceUI(); listen(); }
/* The kiosk asks the opening question out loud, then opens the mic once for the answer. */
function hostAsk(){
  if ($('#welcome').hidden || S.starting) return;
  if (!SR) toast('Voice input needs Chrome or Edge - tap + or – to choose the number of guests.');
  speak(HELLO, listenOnce);
}
function handlePartyAnswer(text){
  $('#heard').textContent = '“' + text + '”';
  const n = parsePartySize(text);
  if (!n){
    S.partyMisses++;
    if (S.partyMisses <= 2){ $('#heard').textContent = "Sorry, I didn't catch that. How many guests?"; speak("Sorry, I didn't catch that. How many guests?", listenOnce); }
    else { $('#heard').textContent = 'Tap + or – to set the number of guests'; S.tap = false; setVoiceUI(); }
    return;
  }
  S.partyMisses = 0; S.party = n; renderParty(); S.tap = false; setVoiceUI();
  setTimeout(startSession, 450);                            // brief pause so the guest sees the number land
}
function onHeard(text){ if (!$('#welcome').hidden) handlePartyAnswer(text); else sendText(text, true); }

async function startSession(){
  if (S.starting) return;
  S.starting = true;
  stopListening(); cancelSpeech(); S.tap = false;
  try {
    const data = await api('/api/init', {table_no: S.table, party_size: S.party});
    S.sid = data.session_id; S.table = data.table_no; S.party = data.party_size; S.fsm = data.current_state;
    $('#welcome').hidden = true;
    $('#stream').replaceChildren(); S.cat = null; S.sub = null; renderRibbon(); renderParty();
    addWaiter(data.greeting);                               // "... May I take your order?"
    applySnapshot(data);
    speak(data.greeting, listenOnce);                       // spoken automatically, then the mic opens once for the answer
  } catch (e) {
    toast('Could not start your table. Is the kiosk server still running?');
  } finally { S.starting = false; setVoiceUI(); }
}

/* ===================== conversation ===================== */
const RIBBON_COPIES = 4;   // the chip set is repeated so the ribbon can loop forever
function renderRibbon(){
  const rb = $('#ribbon');
  if (!rb.children.length){   // build once: re-building would reset the scroll position on every update
    const chip = (c, copy) => el('button', Object.assign({
      class:'chip', type:'button', 'data-cat':c, 'aria-pressed':'false',
      onclick: () => { S.cat = c; S.sub = null; renderRibbon(); sendText(`Show me the ${c} menu`); }
    }, copy ? {'aria-hidden':'true', tabindex:'-1'} : {}), el('span', {class:'em', text: ICON[c]}), c);
    rb.replaceChildren(...Array.from({length: RIBBON_COPIES}, (_, k) => CATS.map(c => chip(c, k > 0))).flat());
  }
  rb.querySelectorAll('.chip').forEach(b => b.setAttribute('aria-pressed', S.cat === b.dataset.cat ? 'true' : 'false'));
  // second row: Beverages > All / Coffee / Non-Coffee / Refreshers
  const sr = $('#subRibbon'), show = S.cat === 'Beverages';
  sr.hidden = !show;
  sr.replaceChildren(...(show ? [['All','🥤'], ...SUBS].map(([s, em]) => el('button', {
    class:'chip', type:'button', 'aria-pressed': (S.sub || 'All') === s ? 'true' : 'false',
    onclick: () => { S.sub = s === 'All' ? null : s; renderRibbon(); sendText(`Show me the ${s === 'All' ? 'Beverages' : s} menu`); }
  }, el('span', {class:'em', text: em}), s)) : []));
}
/* ---- looping ribbon: drifts left forever; hover / touch / drag / wheel / keyboard focus pause it,
   the arrows nudge it ---- */
const Ribbon = {pos:0, last:0, t:0, ready:false, userPaused:false, hover:false, focus:false, down:false, drag:false,
                hold:0, kick:0, speed:36, dx0:0, sl0:0, moved:false};   // speed = pixels per second
function ribbonTick(ts){
  requestAnimationFrame(ribbonTick);
  const rb = $('#ribbon'), R = Ribbon;
  const dt = Math.min(0.1, (ts - (R.t || ts)) / 1000); R.t = ts;
  const first = rb.children[0], next = rb.children[CATS.length];
  const w = first && next ? next.offsetLeft - first.offsetLeft : 0;   // width of ONE chip set
  if (w <= 0){ R.ready = false; return; }                              // welcome screen still hiding the ribbon
  if (!R.ready){ R.ready = true; rb.scrollLeft = R.pos = R.last = w; return; }
  if (Math.abs(rb.scrollLeft - R.last) > 1.5){ R.pos = rb.scrollLeft; R.hold = ts + 2500; }   // the guest moved it
  const blocked = R.userPaused || R.hover || R.focus || R.down || R.drag || ts < R.hold;
  let move = blocked ? 0 : R.speed * dt;
  if (R.kick){ const s = R.kick * Math.min(1, dt * 7); R.kick -= s; if (Math.abs(R.kick) < 0.5) R.kick = 0; move += s; }
  const before = R.pos;
  R.pos += move;
  if (R.pos >= 2 * w) R.pos -= w; else if (R.pos < w) R.pos += w;    // stay inside the middle copies = seamless loop
  if (R.drag) R.sl0 += R.pos - before - move;                          // keep an active drag consistent across a wrap
  if (move !== 0 || Math.abs(R.pos - rb.scrollLeft) > 1.5) rb.scrollLeft = R.pos;
  R.last = rb.scrollLeft;
}
function initRibbon(){
  const rb = $('#ribbon'), bar = $('#ribbonBar'), R = Ribbon, now = () => performance.now();
  R.userPaused = !!(window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches);
  $('#rbPrev').onclick = () => { R.kick -= 340; R.hold = now() + 2500; };
  $('#rbNext').onclick = () => { R.kick += 340; R.hold = now() + 2500; };
  bar.addEventListener('pointerenter', e => { if (e.pointerType === 'mouse') R.hover = true; });
  bar.addEventListener('pointerleave', () => { R.hover = false; R.hold = now() + 600; });
  bar.addEventListener('focusin', e => { R.focus = !!(e.target.matches && e.target.matches(':focus-visible')); });
  bar.addEventListener('focusout', () => { R.focus = false; });
  rb.addEventListener('wheel', e => { e.preventDefault(); rb.scrollLeft += Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY; }, {passive:false});
  // mouse drag (touch screens scroll natively)
  rb.addEventListener('pointerdown', e => { if (e.pointerType !== 'mouse' || e.button !== 0) return; R.down = true; R.dx0 = e.clientX; R.sl0 = rb.scrollLeft; R.moved = false; });
  rb.addEventListener('pointermove', e => {
    if (!R.down) return;
    const dx = e.clientX - R.dx0;
    if (!R.drag && Math.abs(dx) > 6){ R.drag = true; R.moved = true; rb.setPointerCapture(e.pointerId); rb.classList.add('dragging'); }
    if (R.drag) rb.scrollLeft = R.sl0 - dx;
  });
  const end = () => { if (!R.down) return; R.down = false; if (R.drag){ R.drag = false; rb.classList.remove('dragging'); R.hold = now() + 2500; } };
  ['pointerup', 'pointercancel', 'lostpointercapture'].forEach(ev => rb.addEventListener(ev, end));
  rb.addEventListener('click', e => { if (R.moved){ e.stopPropagation(); e.preventDefault(); R.moved = false; } }, true);   // a drag is not a tap
  requestAnimationFrame(ribbonTick);
}
function scrollDown(){ const s = $('#stream'); s.scrollTop = s.scrollHeight; }
function addGuest(text, byVoice){
  $('#stream').append(el('div', {class:'msg guest'}, el('div', {class:'who', text: byVoice ? 'You · by voice' : 'You'}), text)); scrollDown();
}
function dishCard(it, picker, onImg){
  const out = it.stock_qty <= 0, low = !out && it.stock_qty <= 5;
  const arch = el('div', {class:'arch'}, el('div', {class:'tile', text: SUBICON[it.subcategory] || ICON[it.category] || '🍽️'}));
  let archImg = null;   // photo follows the Hot / Iced toggle when the drink has one photo per temperature
  const photoFor = t => (t === 'Hot' ? it.image_hot : t === 'Iced' ? it.image_iced : '') || it.image_url || '';
  const showPhoto = t => {
    const u = photoFor(t); if (!u) return;
    if (archImg) archImg.src = u;
    else { archImg = el('img', {src:u, alt:it.name, loading:'lazy'}); archImg.addEventListener('load', () => arch.classList.add('photo')); archImg.addEventListener('error', () => { archImg.remove(); archImg = null; arch.classList.remove('photo'); }); arch.append(archImg); }
    if (onImg) onImg(u);
  };
  const allergens = (it.allergens || '').split(',').map(s => s.trim()).filter(Boolean);
  // hot / iced choice (Coffee and Non-Coffee drinks; Iced is the default) + size (all beverages; Tall is the default)
  const opts = (it.temp_options || '').split(',').map(s => s.trim()).filter(Boolean);
  let temp = opts.length ? (opts.includes('iced') ? 'Iced' : 'Hot') : '';
  let size = it.size_menu ? (it.default_size || 'Tall') : '';
  const priceEl = el('div', {class:'price', text: money(it.price)});
  const sizeBox = el('div', {class:'temp', role:'group', 'aria-label':'Size'});
  const tempBox = el('div', {class:'temp', role:'group', 'aria-label':'Hot or iced'});
  const paint = () => {
    showPhoto(temp);
    tempBox.replaceChildren(...(opts.length > 1 ? opts.map(o => {
      const label = o === 'hot' ? 'Hot' : 'Iced';
      return el('button', {class:'tbtn', type:'button', 'aria-pressed': temp === label ? 'true' : 'false',
        onclick: () => { temp = label; paint(); }}, (o === 'hot' ? '☕ ' : '🧊 ') + label);
    }) : []));
    const list = it.size_menu ? (it.size_menu[temp] || it.size_menu[''] || []) : [];
    if (list.length && !list.includes(size)) size = list.includes('Tall') ? 'Tall' : list[0];
    sizeBox.replaceChildren(...list.map(sz => el('button', {class:'tbtn', type:'button', 'aria-pressed': size === sz ? 'true' : 'false',
      onclick: () => { size = sz; paint(); }}, sz)));
    const unit = Number(it.price) + Number((it.size_deltas || {})[size] || 0);
    priceEl.textContent = unit === 0 ? 'Free' : money(unit);
  };
  paint();
  return el('article', {class: picker ? 'dish picker' : 'dish'},
    picker ? null : arch,
    el('h3', {text: it.name}),
    priceEl,
    el('div', {class:'meta'},
        out ? el('div', {class:'low', text:'Sold out'}) : low ? el('div', {class:'low', text:`Only ${it.stock_qty} left`}) : null,
        allergens.length ? el('div', {text:'Contains ' + allergens.join(', ')}) : null),
    opts.length > 1 ? tempBox : null,
    it.size_menu ? sizeBox : null,
    el('button', {class:'add', type:'button', disabled: out, 'aria-label':`Add ${it.name} to order`, onclick: () => addDish(it.name, temp, size)},
       el('span', {html:SVG.plus}), out ? 'Sold out' : 'Add to order'));
}
/* ---- menu board: photo cards on the left, title + dotted price list on the right; tap a dish to pick options and add it ---- */
const BOARD_PAGE = 6;
function boardPrice(p){ p = Number(p); if (p === 0) return 'Free'; return 'Php ' + (Number.isInteger(p) ? p : p.toFixed(2)); }
function menuBoard(r){
  const items = r.items, pages = Math.ceil(items.length / BOARD_PAGE);
  const showSub = r.category === 'Beverages' && !r.subcategory;
  let page = 0, sel = null;
  const photos = el('div', {class:'bd-photos'}), list = el('div', {role:'list'}), plates = {};
  const pager = el('div', {class:'bd-pager'}), picker = el('div', {class:'bd-pick'}, el('div', {class:'bd-hint', text:'Tap a dish to see details and add it to your order.'}));
  const mark = () => board.querySelectorAll('[data-dish]').forEach(b => b.setAttribute('aria-pressed', sel && b.dataset.dish === sel.name ? 'true' : 'false'));
  const choose = it => { sel = it; picker.replaceChildren(dishCard(it, true, u => {   // swap the board photo when Hot / Iced is toggled
    const plate = plates[it.name]; if (!plate) return;
    let img = plate.querySelector('img');
    if (img) img.src = u;
    else { img = el('img', {src:u, alt:'', loading:'lazy'}); img.addEventListener('error', () => img.remove()); img.addEventListener('load', () => plate.classList.add('photo')); plate.append(img); }
  })); mark(); picker.scrollIntoView({block:'nearest', behavior:'smooth'}); };
  const draw = () => {
    const slice = items.slice(page * BOARD_PAGE, (page + 1) * BOARD_PAGE);
    const cls = it => it.stock_qty <= 0 ? ' out' : '';
    photos.replaceChildren(...slice.map(it => {
      const plate = plates[it.name] = el('div', {class:'bd-plate'}, el('div', {class:'tile', text: SUBICON[it.subcategory] || ICON[it.category] || '🍽️'}));
      if (it.image_url){ const img = el('img', {src:it.image_url, alt:'', loading:'lazy'}); img.addEventListener('error', () => img.remove()); img.addEventListener('load', () => plate.classList.add('photo')); plate.append(img); }
      return el('button', {class:'bd-card' + cls(it), type:'button', 'data-dish':it.name, 'aria-pressed':'false', 'aria-label':`${it.name}, ${boardPrice(it.price)}`,
                           onclick: () => choose(it)}, plate, el('span', {class:'bd-name', text: it.name}));
    }));
    list.replaceChildren(...slice.map(it => el('button', {class:'bd-row' + cls(it), type:'button', role:'listitem', 'data-dish':it.name, 'aria-pressed':'false', onclick: () => choose(it)},
      el('span', {}, it.name, showSub && it.subcategory ? el('small', {text: it.subcategory}) : null),
      el('span', {class:'lead'}), el('span', {text: boardPrice(it.price)}))));
    pager.hidden = pages < 2;
    pager.replaceChildren(
      el('button', {type:'button', 'aria-label':'Previous page', disabled: page === 0, onclick: () => { page--; draw(); }}, '‹'),
      el('span', {text:`Page ${page + 1} of ${pages}`}),
      el('button', {type:'button', 'aria-label':'Next page', disabled: page >= pages - 1, onclick: () => { page++; draw(); }}, '›'));
    mark();
  };
  const board = el('div', {class:'board'}, photos,
    el('div', {class:'bd-info'}, el('div', {class:'bd-title', text: (r.subcategory || r.category || '').toLowerCase()}), el('div', {class:'bd-pill', text:'Menu'}), list, pager));
  draw();
  return el('div', {}, board, picker);
}
function addWaiter(text, toolCalls){
  const body = el('div', {class:'msg waiter'}, el('div', {class:'who', text:'Waiter'}), el('div', {text}));
  let hasMenu = false;
  (toolCalls || []).forEach(tc => {
    if (tc.tool === 'get_menu_by_category' && tc.result && tc.result.success){
      const r = tc.result;
      if (r.items && r.items.length) body.append(menuBoard(r));
      S.cat = r.category; S.sub = r.subcategory || null; renderRibbon(); hasMenu = true;
    }
  });
  if (hasMenu) body.classList.add('wide');
  $('#stream').append(body);
  if (hasMenu) body.scrollIntoView({block:'start'}); else scrollDown();   // show a new menu from its first row
  return body;
}
async function sendText(msg, viaVoice){
  msg = (msg || '').trim();
  if (!msg || !S.sid) return;
  if (S.busy){ toast('One moment - your waiter is still answering.'); return; }
  const prevFsm = S.fsm;
  S.busy = true; S.tap = false; cancelSpeech(); stopListening(); setVoiceUI();
  addGuest(msg, viaVoice);
  const typing = el('div', {class:'msg waiter'}, el('div', {class:'who', text:'Waiter'}), el('span', {class:'typing'}, el('i'), el('i'), el('i')));
  $('#stream').append(typing); scrollDown();
  try {
    const data = await api('/api/chat', {session_id:S.sid, message:msg});
    typing.remove();
    const justConfirmed = data.current_state === 'CONFIRMED' && data.receipt && S.fsm !== 'CONFIRMED';   // "confirm" said out loud
    addWaiter(data.assistant_reply, data.tool_calls);
    applySnapshot(data);
    if (justConfirmed) showReceipt(data.receipt);
    const enteredReview = data.current_state === 'REVIEW' && prevFsm !== 'REVIEW';   // Aeris always reads out the review and the confirmation
    if (S.voice || viaVoice || enteredReview || justConfirmed) speak(justConfirmed ? 'Your order is on its way to the kitchen. Thank you, and enjoy your meal!'
                                                 : spokenReply(data.assistant_reply, data.tool_calls));
  } catch (e) {
    typing.remove();
    if (e.message !== 'session_expired') addWaiter('I could not reach the kitchen system. Please try again in a moment.');
  } finally { S.busy = false; setVoiceUI(); if (S.voice && !S.speaking) listen(); }
}
async function addDish(name, instructions, size){
  const d = await api('/api/manual_override', {session_id:S.sid, action:'add', item_name:name, instructions: instructions || '', size: size || ''});
  if (!d.ok) toast(d.message || 'Could not add that item.');
  applySnapshot(d);
}

/* ===================== cart ===================== */
function thumb(i){
  const t = el('div', {class:'thumb'}, el('div', {class:'tile', text: ICON[i.category] || '🍽️'}));
  if (i.image_url){ const img = el('img', {src:i.image_url, alt:''}); img.addEventListener('load', () => t.classList.add('photo')); img.addEventListener('error', () => img.remove()); t.append(img); }
  return t;
}
function renderCart(){
  const o = S.order, box = $('#lines');
  $('#cartCount').textContent = o.item_count ? `${o.item_count} item${o.item_count > 1 ? 's' : ''}` : 'Updates as you order';
  $('#subVal').textContent = money(o.subtotal);
  $('#feeVal').textContent = money(o.service_fee_10_pct);
  $('#grandVal').textContent = money(o.grand_total);
  if (!o.items.length){
    box.replaceChildren(el('div', {class:'empty'}, el('div', {html:SVG.leaf, style:'margin:0 auto'}),
      el('p', {text:'Nothing here yet. Choose a category and tap Add to order, or just tell your waiter.'})));
    return;
  }
  const locked = S.fsm === 'CONFIRMED' || S.fsm === 'CANCELLED';
  box.replaceChildren(...o.items.map(i => {
    const badges = (i.special_instructions || '').split(',').map(s => s.trim()).filter(Boolean);
    const row = el('div', {class:'line'},
      thumb(i),
      el('div', {}, el('h4', {text:i.display_name || i.name}), el('div', {class:'each', text: money(i.price) + ' each'}),
        badges.length ? el('div', {class:'badges'}, badges.map(b => el('span', {class:'badge', text:b}))) : null),
      el('div', {class:'total', text: money(i.line_total)}));
    if (!locked){
      row.append(el('div', {class:'actions'},
        el('div', {class:'qty'},
          el('button', {type:'button', 'aria-label':`Remove one ${i.name}`, html:SVG.minus, onclick:() => cartAct('dec', i.line_id)}),
          el('output', {text:i.quantity, 'aria-label':'quantity'}),
          el('button', {type:'button', 'aria-label':`Add one ${i.name}`, html:SVG.plus, onclick:() => cartAct('inc', i.line_id)})),
        el('button', {class:'link', type:'button', text: badges.length ? 'Edit note' : 'Add note', onclick:() => { S.editing = i.line_id; renderCart(); }}),
        el('button', {class:'link', type:'button', text:'Remove', onclick:() => cartAct('remove', i.line_id)})));
      if (S.editing === i.line_id){
        const inp = el('input', {type:'text', value:i.special_instructions || '', maxlength:'120', placeholder:'e.g. No onions, extra cheese', 'aria-label':'Preparation note'});
        const save = () => { S.editing = null; cartAct('note', i.line_id, inp.value); };
        inp.addEventListener('keydown', e => { if (e.key === 'Enter'){ e.preventDefault(); save(); } if (e.key === 'Escape'){ S.editing = null; renderCart(); } });
        row.append(el('div', {class:'noteform'}, inp,
          el('button', {type:'button', text:'Save', onclick:save}),
          el('button', {type:'button', class:'ghost', text:'Cancel', onclick:() => { S.editing = null; renderCart(); }})));
        setTimeout(() => inp.focus(), 0);
      }
    }
    return row;
  }));
}
async function cartAct(action, lineId, note){
  const d = await api('/api/manual_override', {session_id:S.sid, action, line_id:lineId, note});
  if (!d.ok && d.message) toast(d.message);
  applySnapshot(d);
}

/* ===================== FSM bar ===================== */
function renderBar(){
  const idx = STEPS.findIndex(s => s[0] === S.fsm), cancelled = S.fsm === 'CANCELLED';
  $('#steps').replaceChildren(...(cancelled
    ? [el('li', {class:'cancelled-note', text:'Order cancelled'})]
    : STEPS.map((s, n) => el('li', {class: n < idx ? 'done' : n === idx ? 'now' : '', 'aria-current': n === idx ? 'step' : null}, el('i'), el('span', {text:s[1]})))));
  const empty = !S.order.items.length, cta = $('#cta');
  S.cancelArmed = S.cancelArmed && (S.fsm === 'ORDERING' || S.fsm === 'REVIEW');
  const cancel = (S.fsm === 'ORDERING' || S.fsm === 'REVIEW') ? el('button', {class:'btn quiet' + (S.cancelArmed ? ' armed' : ''), type:'button',
      text: S.cancelArmed ? 'Tap again to cancel' : 'Cancel order', onclick:armCancel}) : null;
  const clear = (S.fsm === 'ORDERING' && !empty) ? el('button', {class:'btn quiet', type:'button', text:'Clear cart', onclick:() => cartAct('clear')}) : null;
  if (S.fsm === 'REVIEW'){
    cta.replaceChildren(...[cancel, el('button', {class:'btn outline', type:'button', text:'Keep ordering', onclick:keepOrdering}),
      el('button', {class:'btn primary', type:'button', disabled: !S.order.payment_method, title: S.order.payment_method ? null : 'Choose a mode of payment first',
        text: S.order.payment_method ? 'Confirm order' : 'Select payment first', onclick:confirmOrder})].filter(Boolean));
  } else if (S.fsm === 'CONFIRMED' || S.fsm === 'CANCELLED'){
    cta.replaceChildren(el('button', {class:'btn primary', type:'button', text:'New order', onclick:() => showWelcome()}));
  } else {
    cta.replaceChildren(...[cancel, clear, el('button', {class:'btn primary', type:'button', disabled: empty, text:'Review order', onclick:reviewOrder})].filter(Boolean));
  }
}
/* Mode of payment: shown only while the order is in REVIEW; Confirm order stays disabled until one is picked. */
function renderPay(){
  const box = $('#payBox'), show = S.fsm === 'REVIEW' && S.order.items.length > 0;
  box.hidden = !show;
  if (!show) return;
  const cur = S.order.payment_method || '', methods = S.order.payment_methods || Object.keys(PAY_ICON);
  $('#payOpts').replaceChildren(...methods.map(m => el('button', {class:'popt', type:'button', role:'radio', 'aria-checked': cur === m ? 'true' : 'false',
    onclick: () => choosePayment(m)}, el('span', {class:'em', text: PAY_ICON[m] || '💳'}), m)));
  const note = $('#payNote');
  note.textContent = cur ? (S.order.payment_note || '') : 'Choose how you would like to pay to confirm your order.';
  note.classList.toggle('need', !cur);
}
async function choosePayment(method){
  const d = await api('/api/payment', {session_id:S.sid, method});
  if (!d.ok && d.message) toast(d.message);
  applySnapshot(d);
  if (d.ok) speak(d.message);                               // Aeris repeats the choice out loud
}
let cancelTimer;
function armCancel(){
  if (!S.cancelArmed){ S.cancelArmed = true; renderBar(); clearTimeout(cancelTimer); cancelTimer = setTimeout(() => { S.cancelArmed = false; renderBar(); }, 3500); return; }
  clearTimeout(cancelTimer); S.cancelArmed = false;
  api('/api/cancel', {session_id:S.sid}).then(d => { applySnapshot(d); addWaiter('Your order has been cancelled. Tap New order whenever you are ready.'); });
}
async function reviewOrder(){
  const d = await api('/api/checkout', {session_id:S.sid});
  if (!d.ok){ toast(d.message); return; }
  applySnapshot(d);
  const o = d.order_snapshot;
  const line = `Here is your order: ${o.item_count} item${o.item_count > 1 ? 's' : ''}, ${money(o.grand_total)} with the service fee. ` +
    (o.payment_method ? `Paying by ${o.payment_method}. Press Confirm order when you are ready, or keep ordering.`
                      : 'How would you like to pay: cash, GCash, Maya, or card? Pick one, then press Confirm order.');
  addWaiter(line);
  speak(line);                                              // Aeris reads the review out loud
}
async function keepOrdering(){ applySnapshot(await api('/api/back', {session_id:S.sid})); }
async function confirmOrder(){
  const d = await api('/api/confirm', {session_id:S.sid});
  applySnapshot(d);
  if (!d.ok){ toast(d.message); addWaiter(d.message); speak(d.message); return; }   // e.g. "Please choose a mode of payment first"
  addWaiter(d.message); showReceipt(d.receipt);
  speak('Your order is on its way to the kitchen. Thank you, and enjoy your meal!');   // Aeris confirms out loud
}

/* ===================== receipt ===================== */
const pad2 = (n) => String(n).padStart(2, '0');
function showReceipt(r){
  if (!r) return;
  const d = new Date(String(r.issued_at || '').replace(' ', 'T'));
  const when = isNaN(d) ? (r.issued_at || '') : d.toLocaleDateString('en-US', {weekday:'long', month:'short', day:'numeric', year:'numeric'}) + ' \u00B7 ' + pad2(d.getHours()) + ':' + pad2(d.getMinutes());
  $('#rcNo').textContent = 'Order #' + r.receipt_no; $('#rcTable').textContent = 'For table ' + r.table_no; $('#rcTime').textContent = when;
  $('#rcItems').replaceChildren(...r.items.map(i => el('div', {class:'rc-item'},
    el('span', {text: pad2(i.quantity)}),
    el('span', {class:'nm', text: i.display_name || i.name}),
    el('span', {class:'amt', text: Number(i.line_total).toFixed(2)}),
    i.special_instructions ? el('span', {class:'note', text: i.special_instructions}) : null)));
  $('#rcCount').textContent = r.items.reduce((n, i) => n + Number(i.quantity), 0);
  $('#rcSub').textContent = Number(r.subtotal).toFixed(2); $('#rcFee').textContent = Number(r.service_fee).toFixed(2); $('#rcTotal').textContent = money(r.grand_total);
  $('#rcParty').textContent = S.party; $('#rcCode').textContent = r.receipt_no; $('#rcPay').textContent = r.payment_method || '-';
  $('#receipt').hidden = false; $('#rcPaper').scrollTop = 0; $('#rcNew').focus();
}
$('#rcPrint').onclick = () => window.print();
$('#rcNew').onclick = () => { $('#receipt').hidden = true; showWelcome(); };

/* ===================== snapshot + polling ===================== */
function applySnapshot(d){
  if (!d) return;
  if (d.current_state) S.fsm = d.current_state;
  if (d.table_no){ S.table = d.table_no; }
  if (d.party_size){ S.party = d.party_size; }
  if (d.order_snapshot) S.order = d.order_snapshot;
  renderChip();
  const key = S.fsm + JSON.stringify(S.order);
  if (key !== S.cartKey){ S.cartKey = key; renderCart(); }
  renderBar(); renderPay();
}
setInterval(async () => {                      // live cart: picks up changes made by the model's tool calls
  if (!S.sid || S.busy || S.editing !== null || !$('#welcome').hidden) return;
  try { applySnapshot(await api('/api/order/' + S.sid)); } catch (e) {}
}, 4000);

/* ===================== voice (Web Speech API) ===================== */
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;     // speech-to-text (Chrome / Edge)
let rec = null, quickFails = 0;
function setVoiceUI(){
  const b = $('#voiceBtn'), lab = $('#voiceLabel'), on = S.voice || S.tap;
  const live = on && !S.busy && !S.speaking;
  b.setAttribute('aria-pressed', on ? 'true' : 'false');
  $('#voiceGrp').classList.toggle('on', on);
  b.classList.toggle('listening', live);
  const idle = S.mode === 'auto' ? 'Auto mic off' : 'Tap to speak';
  lab.textContent = S.busy ? 'Thinking' : S.speaking ? 'Speaking' : on ? (SR && !S.sttBroken ? 'Listening' : 'Voice on') : idle;
  $('#welcome').classList.toggle('talking', S.speaking);                  // the arch glows while the voice plays
  const w = $('#welcomeMic');                                              // the same state on the welcome screen
  w.setAttribute('aria-pressed', on ? 'true' : 'false');
  w.classList.toggle('listening', live); w.classList.toggle('idle', !on && !S.speaking);
  $('#wMicLabel').textContent = S.speaking ? 'Speaking…'
    : on ? (SR && !S.sttBroken ? 'Listening… say how many' : 'Use the + and –') : 'Tap to talk with Aeris';
}
function canListen(){
  if (!SR || !(S.voice || S.tap) || S.sttBroken || S.busy || S.speaking || rec || S.typed) return false;   // never listen while typing
  if (!$('#welcome').hidden) return true;
  return !!S.sid && S.fsm !== 'CONFIRMED' && S.fsm !== 'CANCELLED' && $('#receipt').hidden;
}
function listen(){
  if (!canListen()) return;
  const r = new SR(); rec = r;
  r.lang = 'en-US'; r.interimResults = true; r.continuous = false;
  let finalText = '', lastText = '', endTimer; const startedAt = Date.now();
  r.onresult = (e) => {
    if (S.typed) return;                                   // typed text always wins over the mic
    let t = ''; for (const x of e.results){ t += x[0].transcript; if (x.isFinal) finalText = t; }
    lastText = t;
    if (!$('#welcome').hidden) $('#heard').textContent = '“' + t + '”'; else $('#userInput').value = t;
    clearTimeout(endTimer); endTimer = setTimeout(() => { try { r.stop(); } catch (x) {} }, 1100);   // finish quickly after the guest stops
  };
  r.onerror = (e) => {
    if (e.error === 'not-allowed' || e.error === 'service-not-allowed'){
      S.voice = false; S.tap = false; S.sttBroken = true; toast('Microphone access is blocked. Allow it in your browser to talk to your waiter.');
    } else if (e.error === 'network'){
      S.sttBroken = true; S.tap = false; toast('Speech recognition could not reach its service. You can still type or tap.');
    }
    setVoiceUI();
  };
  r.onend = () => {
    clearTimeout(endTimer);
    if (rec !== r) return;                                 // stopped on purpose (the waiter is about to speak)
    rec = null;
    const heard = (finalText || lastText).trim();
    if (heard && !S.typed){
      $('#userInput').value = '';
      if (isEcho(heard)){                                  // that was the kiosk hearing its own voice, not the guest
        quickFails = 0; if (!$('#welcome').hidden) $('#heard').textContent = '';
        setTimeout(listen, 250); return;
      }
      quickFails = 0; S.tap = false; onHeard(heard); return;
    }
    quickFails = (Date.now() - startedAt < 1000) ? quickFails + 1 : 0;   // ending instantly = the mic is not really working
    if (quickFails >= 4){ S.sttBroken = true; S.tap = false; toast("I can't hear the microphone. You can still type or tap."); }
    else if (S.voice && !S.busy && !S.speaking) setTimeout(listen, 400);     // automatic mic keeps listening
    else S.tap = false;                                                      // tap mode: one shot, then rest
    setVoiceUI();
  };
  try { r.start(); } catch (e) { rec = null; S.tap = false; }
  setVoiceUI();
}
function stopListening(){ if (rec){ const r = rec; rec = null; try { r.abort(); } catch (e) {} } }

/* The mic can hear the speakers. Anything that matches what the waiter just said is the kiosk hearing itself. */
function norm(t){ return String(t).toLowerCase().replace(/[^a-z0-9\s]/g, ' ').replace(/\s+/g, ' ').trim(); }
function isEcho(heard){
  const h = norm(heard); if (!h) return true;
  const hw = h.split(' '), now = Date.now();
  return S.said.some(u => {
    if (now - u.end > 8000) return false;                                   // too long ago to be an echo
    if (hw.length === 1) return now - u.end < 1500 && u.text.split(' ').includes(h);
    if (u.text.includes(h)) return true;                                    // a fragment of what was said
    const sw = new Set(u.text.split(' '));
    return hw.length >= 3 && hw.filter(w => sw.has(w)).length / hw.length >= 0.7;   // same words, recogniser typos
  });
}

/* ---- text-to-speech ---- */
let voicePick = null, speakId = 0, speakTimer;
function pickVoice(){
  const en = speechSynthesis.getVoices().filter(v => /^en/i.test(v.lang));
  for (const p of [/natural.*(aria|jenny|sonia|libby)/i, /\b(aria|jenny)\b/i, /google us english/i, /samantha|serena|karen|moira/i, /zira|hazel|susan/i]){
    const v = en.find(v => p.test(v.name)); if (v){ voicePick = v; return; }
  }
  voicePick = en.find(v => v.default) || en[0] || null;
}
if ('speechSynthesis' in window){ pickVoice(); speechSynthesis.onvoiceschanged = pickVoice; }
/* Make text sound natural for text-to-speech: the engine only pauses at punctuation,
   so add commas / periods where the written text relies on layout instead. */
function prepSpeech(text){
  let t = String(text).replace(/[\u{1F000}-\u{1FFFF}\u{2600}-\u{27BF}]/gu, '');
  t = t.replace(/PHP\s?([\d,]+(?:\.\d+)?)/gi, (m, n) => n.replace(/\.00$/, '') + ' pesos')
       .replace(/\s*[\r\n]+\s*/g, '. ')              // line breaks -> sentence pause
       .replace(/\s+[-–—]\s+/g, ', ')                // " - " dashes -> comma pause
       .replace(/\s*[:;]\s+/g, ', ')                  // colons / semicolons -> comma pause
       .replace(/\s*\+\s*/g, ' plus ')                // "+" is read as nothing by some voices
       .replace(/([!?])(?=\S)/g, '$1 ')
       .replace(/\.{2,}/g, '.')
       .replace(/,\s*,/g, ',')
       .replace(/\s{2,}/g, ' ').trim();
  if (t && !/[.!?]$/.test(t)) t += '.';               // always end with a full stop
  return t;
}
/* When the waiter shows a category, read the dish names as a comma-separated list. */
function spokenReply(reply, toolCalls){
  const menu = (toolCalls || []).find(tc => tc.tool === 'get_menu_by_category' && tc.result && tc.result.success);
  if (!menu) return reply;
  const names = menu.result.items.map(i => i.name);
  const list = names.length > 1 ? names.slice(0, -1).join(', ') + ', and ' + names[names.length - 1] : names.join('');
  return `Here is our ${menu.result.category} menu. We have ${list}. Tap Add to order on anything you would like.`;
}
function cancelSpeech(){
  speakId++; clearTimeout(speakTimer); S.speaking = false;
  S.said.forEach(u => { if (u.end === Infinity) u.end = Date.now(); });
  if ('speechSynthesis' in window) speechSynthesis.cancel();
}
function stopSpeech(){ cancelSpeech(); }
/* Speak sentence by sentence (a real gap between sentences even if the voice ignores punctuation), then run `then`
   (or reopen the mic in automatic mode). The mic is closed while talking, and reopens a moment after the last word. */
function speak(text, then){
  const clean = prepSpeech(text);
  const after = () => { if (then) then(); else if (S.voice) listen(); };
  if (!clean || !('speechSynthesis' in window)){ setTimeout(after, 0); return; }
  stopListening();
  const id = ++speakId;
  speechSynthesis.cancel();
  let parts; try { parts = clean.split(/(?<=[.!?])\s+/).filter(Boolean); } catch (e) { parts = [clean]; }
  S.speaking = true; setVoiceUI();                                        // set NOW, so nothing opens the mic while we talk
  const said = {text: norm(clean), end: Infinity};
  S.said.push(said); if (S.said.length > 4) S.said.shift();
  const t0 = Date.now(); let finished = false;
  const finish = () => {
    if (finished || id !== speakId) return;                               // a newer utterance replaced this one
    finished = true; clearTimeout(speakTimer); S.speaking = false; said.end = Date.now(); setVoiceUI();
    setTimeout(() => { if (id === speakId) after(); }, 400);              // let the room echo die down first
  };
  const guard = () => {                                                   // some browsers never fire onend
    if (finished || id !== speakId) return;
    if (speechSynthesis.speaking && Date.now() - t0 < 40000) speakTimer = setTimeout(guard, 500); else finish();
  };
  parts.forEach((p, i) => {
    const u = new SpeechSynthesisUtterance(p.trim());
    if (voicePick){ u.voice = voicePick; u.lang = voicePick.lang; } else u.lang = 'en-US';
    u.onerror = finish;
    if (i === parts.length - 1) u.onend = finish;
    speechSynthesis.speak(u);
  });
  speakTimer = setTimeout(guard, 3000 + clean.split(/\s+/).length * 550);
}

$('#voiceBtn').onclick = () => {
  if (!SR && S.mode === 'tap'){ toast('Voice input needs Chrome or Edge - you can still type.'); return; }
  if (S.mode === 'tap'){
    if (S.tap){ if (rec) rec.stop(); return; }              // tap again = finish speaking now
    S.sttBroken = false; stopSpeech(); S.tap = true; setVoiceUI(); listen();
    return;
  }
  S.voice = !S.voice;                                        // automatic mic: on / off
  if (!S.voice){ stopListening(); stopSpeech(); }
  else { S.sttBroken = false; if (!SR) toast('Spoken replies are on. Voice input needs Chrome or Edge - you can still type.'); }
  setVoiceUI(); if (S.voice) listen();
};
$('#welcomeMic').onclick = () => {                           // welcome screen: tap = ask out loud and listen; tap again = stop
  if (S.speaking || S.tap || rec){ stopListening(); cancelSpeech(); S.tap = false; setVoiceUI(); return; }
  S.sttBroken = false; S.partyMisses = 0; hostAsk();
};
function setMode(m){
  S.mode = m; S.voice = false; S.tap = false; stopListening(); stopSpeech();
  document.querySelectorAll('#voiceMenu button').forEach(b => b.setAttribute('aria-checked', b.dataset.mode === m ? 'true' : 'false'));
  closeVoiceMenu(); setVoiceUI();
  toast(m === 'auto' ? 'Automatic mic: press the mic to start hands-free listening.' : 'Tap to speak: press the mic each time you want to talk.');
}
function closeVoiceMenu(){ $('#voiceMenu').hidden = true; $('#voiceCaret').setAttribute('aria-expanded', 'false'); }
$('#voiceCaret').onclick = (e) => {
  e.stopPropagation();
  const m = $('#voiceMenu'); m.hidden = !m.hidden;
  $('#voiceCaret').setAttribute('aria-expanded', m.hidden ? 'false' : 'true');
};
document.querySelectorAll('#voiceMenu button').forEach(b => b.onclick = () => setMode(b.dataset.mode));
document.addEventListener('click', (e) => { if (!e.target.closest('#voiceGrp')) closeVoiceMenu(); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeVoiceMenu(); });

/* ===================== wiring ===================== */
$('#composer').addEventListener('submit', (e) => { e.preventDefault(); const i = $('#userInput'); const v = i.value; i.value = ''; S.typed = false; sendText(v); });
/* Typing takes over from the mic: stop listening so speech results can't overwrite or clear the text. */
$('#userInput').addEventListener('input', () => {
  S.typed = $('#userInput').value.length > 0;
  if (S.typed && rec){ stopListening(); S.tap = false; setVoiceUI(); }
  if (!S.typed && S.voice) listen();
});
$('#tMinus').innerHTML = SVG.minus; $('#tPlus').innerHTML = SVG.plus;
$('#voiceIcon').innerHTML = SVG.mic; $('#wMicIcon').innerHTML = SVG.mic; $('#caretIcon').innerHTML = SVG.chev; $('#sendIcon').innerHTML = SVG.send;
$('#tMinus').onclick = () => { S.party = Math.max(1, S.party - 1); renderParty(); };
$('#tPlus').onclick = () => { S.party = Math.min(MAX_PARTY, S.party + 1); renderParty(); };
$('#startBtn').onclick = startSession;
renderRibbon(); initRibbon(); renderCart(); renderBar(); setVoiceUI(); renderParty();
showWelcome();
</script>
</body>
</html>
"""


# ============================== API ==============================
def _need_session():
    session = agent.get_session((request.json or {}).get("session_id") if request.method == "POST" else None)
    return session


def _expired():
    return jsonify({"ok": False, "error": "session_expired"}), 410


@app.route("/")
def index():
    return Response(HTML_KIOSK, mimetype="text/html")


BASE_DIR = os.path.dirname(os.path.abspath(__file__))


@app.route("/aeris.png")
def aeris_image():
    """Welcome-screen artwork: keep aeris.png next to this file (or in a static/ folder)."""
    for folder in (BASE_DIR, os.path.join(BASE_DIR, "static")):
        if os.path.isfile(os.path.join(folder, "aeris.png")):
            return send_from_directory(folder, "aeris.png")
    return Response("aeris.png not found", status=404)


IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif")


def _img_dirs():
    """Folders searched for dish photos: static/menu, static, then every sub-folder next to this file (pasta/, pastries/, ...)."""
    dirs = [os.path.join(BASE_DIR, "static", "menu"), os.path.join(BASE_DIR, "static")]
    for e in sorted(os.scandir(BASE_DIR), key=lambda e: e.name):
        if e.is_dir() and not e.name.startswith((".", "_")) and e.name != "static":
            dirs.append(e.path)
    return dirs


@app.route("/menu-img/<path:filename>")
def menu_image(filename):
    """Dish photos referenced by menu_items.image_url (image files only, so code and the database stay private)."""
    if filename.lower().endswith(IMG_EXT):
        for folder in _img_dirs():
            if os.path.isfile(os.path.join(folder, filename)):
                return send_from_directory(folder, filename)
    return Response("image not found", status=404)


TABLE_COUNT = 20             # tables in the restaurant (1..TABLE_COUNT)


@app.route("/api/assign_table")
def api_assign_table():
    """Aeris seats each new party at a random table that has no open order."""
    busy = {s.table_no for s in agent.active_sessions.values() if s.current_state not in FINAL_STATES}
    free = [n for n in range(1, TABLE_COUNT + 1) if n not in busy] or list(range(1, TABLE_COUNT + 1))
    return jsonify({"ok": True, "table_no": random.choice(free)})


@app.route("/api/init", methods=["POST"])
def api_init():
    data = request.json or {}
    session = agent.start_session(table_no=data.get("table_no", 4), party_size=data.get("party_size", 2))
    return jsonify({"ok": True, **agent.snapshot(session), "greeting": session.greeting})


@app.route("/api/chat", methods=["POST"])
def api_chat():
    session = _need_session()
    if not session:
        return _expired()
    return jsonify(agent.process_turn(session, (request.json or {}).get("message", "")))


@app.route("/api/order/<session_id>")
def api_order(session_id):
    session = agent.get_session(session_id)
    if not session:
        return _expired()
    return jsonify(agent.snapshot(session))


@app.route("/api/manual_override", methods=["POST"])
def api_manual_override():
    """Human-in-the-loop cart controls - bypass Qwen so +/- is instant."""
    session = _need_session()
    if not session:
        return _expired()
    data = request.json or {}
    action, sid = data.get("action"), session.session_id
    line_id = data.get("line_id")
    if action == "add":
        res = db_tools.add_item_to_order(sid, data.get("item_name", ""), 1, data.get("instructions", ""), data.get("size", ""))
    elif action == "inc":
        res = db_tools.adjust_line_quantity(sid, line_id, +1)
    elif action == "dec":
        res = db_tools.adjust_line_quantity(sid, line_id, -1)
    elif action == "remove":
        res = db_tools.remove_line(sid, line_id)
    elif action == "note":
        res = db_tools.set_line_note(sid, line_id, data.get("note", ""))
    elif action == "clear":
        res = db_tools.clear_order(sid)
    else:
        res = {"success": False, "message": f"Unknown action '{action}'."}
    if res.get("success"):
        agent.note_manual_change(session)
    return jsonify({"ok": bool(res.get("success")), "message": res.get("message", ""), **agent.snapshot(session)})


@app.route("/api/checkout", methods=["POST"])
def api_checkout():
    session = _need_session()
    if not session:
        return _expired()
    res = agent.request_review(session)
    agent.log_ui_event(session, "Review order", res)
    return jsonify({**res, **agent.snapshot(session)})


@app.route("/api/payment", methods=["POST"])
def api_payment():
    """Guest picks the mode of payment (Cash / GCash / Maya / Credit/Debit Card) while reviewing the order."""
    session = _need_session()
    if not session:
        return _expired()
    res = agent.set_payment(session, (request.json or {}).get("method", ""))
    agent.log_ui_event(session, "Mode of payment: " + (res.get("payment_method") or "-"), res)
    return jsonify({**res, **agent.snapshot(session)})


@app.route("/api/back", methods=["POST"])
def api_back():
    session = _need_session()
    if not session:
        return _expired()
    res = agent.back_to_ordering(session)
    agent.log_ui_event(session, "Keep ordering", res)
    return jsonify({**res, **agent.snapshot(session)})


@app.route("/api/confirm", methods=["POST"])
def api_confirm():
    session = _need_session()
    if not session:
        return _expired()
    res = agent.confirm_order(session)
    agent.log_ui_event(session, "Confirm order", res)
    return jsonify({**res, **agent.snapshot(session)})


@app.route("/api/cancel", methods=["POST"])
def api_cancel():
    session = _need_session()
    if not session:
        return _expired()
    res = agent.cancel_order(session)
    agent.log_ui_event(session, "Cancel order", res)
    return jsonify({**res, **agent.snapshot(session)})


def _check_ollama() -> None:
    base = OLLAMA_CHAT_URL.rsplit("/api/", 1)[0]
    try:
        tags = requests.get(base + "/api/tags", timeout=2).json()
        names = [m.get("name", "") for m in tags.get("models", [])]
        if not any(n.startswith(agent.model_name) for n in names):
            print(f"[kiosk] Ollama is running but '{agent.model_name}' is not pulled. Run: ollama pull {agent.model_name}")
        else:
            print(f"[kiosk] Ollama ready with {agent.model_name}.")
    except requests.exceptions.RequestException:
        print("[kiosk] Ollama is not reachable at", base, "- start it with `ollama serve`. "
              "Browsing and the +/- cart still work without it.")


if __name__ == "__main__":
    db_tools.init_database()
    _check_ollama()
    agent.warm_up()          # pre-load the model so the first answer is fast
    threading.Timer(1.0, lambda: webbrowser.open("http://localhost:5000")).start()
    app.run(host="127.0.0.1", port=5000, debug=False)