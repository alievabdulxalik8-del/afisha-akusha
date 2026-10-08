/* Закладка «Продажи → афиша». Запускается на странице кабинета new-report.kassir.ru, где вы уже вошли
   (проверку «я не робот» проходите сами). Делает то же, что раньше делала облачная функция:
   заказывает «Отчёт по продажам (организатор)» по всем событиям, читает итоги и отправляет цифры на афишу.
   Пароль и вход никуда не передаются — только числа продаж. */
(function(){
  var API="https://functions.yandexcloud.net/d4e8n3j9esf1uq386ik3";
  var SITE="https://alievabdulxalik8-del.github.io/afisha-akusha/";
  var F="CommonOrganizerForm", CODE_KEY="afishaCode";

  var box=document.getElementById("afishaPush");
  if(box) box.remove();
  box=document.createElement("div"); box.id="afishaPush";
  box.style.cssText="position:fixed;z-index:2147483647;right:16px;bottom:16px;width:340px;max-width:calc(100vw - 32px);"+
    "background:#1E1C1A;color:#F3EEE8;border-radius:16px;padding:14px 16px;font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;"+
    "box-shadow:0 18px 50px -12px rgba(0,0,0,.5)";
  var head=document.createElement("b"); head.textContent="Продажи → афиша"; head.style.cssText="display:block;font-size:16px;margin-bottom:6px";
  var msg=document.createElement("div");
  var close=document.createElement("button"); close.textContent="×"; close.setAttribute("aria-label","Закрыть");
  close.style.cssText="position:absolute;top:6px;right:10px;background:none;border:0;color:#A8A098;font-size:22px;cursor:pointer";
  close.onclick=function(){ box.remove(); };
  box.appendChild(close); box.appendChild(head); box.appendChild(msg); document.body.appendChild(box);
  function say(t, color){ msg.textContent=t; msg.style.color=color||"#F3EEE8"; }
  function done(t){ say(t,"#7FD39B"); var a=document.createElement("a"); a.href=SITE; a.target="_blank"; a.textContent="Открыть афишу";
    a.style.cssText="display:inline-block;margin-top:10px;color:#F2A65A;font-weight:600"; box.appendChild(a); }
  function fail(t){ say(t,"#F0907F"); }

  if(location.hostname!=="new-report.kassir.ru"){ fail("Откройте кабинет new-report.kassir.ru, войдите и нажмите закладку там."); return; }

  function get(path, ajax){
    return fetch(path,{credentials:"same-origin",headers:ajax?{"X-Requested-With":"XMLHttpRequest"}:{}})
      .then(function(r){ return r.text().then(function(t){ return {url:r.url, text:t}; }); });
  }
  function csrf(html){ var m=html.match(/name="_csrf-report" value="([^"]*)"/); if(!m) throw new Error("csrf"); return m[1]; }
  function sleep(ms){ return new Promise(function(r){ setTimeout(r,ms); }); }

  function events(){
    return get("/search/event/index",true).then(function(r){
      var j; try{ j=JSON.parse(r.text); }catch(e){ throw new Error("login"); }
      return j.data.rows.map(function(x){ return x.id; });
    });
  }
  function report(ids){
    return get("/report/request/create?report_id=1").then(function(r){
      var f=new URLSearchParams();
      f.append("_csrf-report",csrf(r.text)); f.append(F+"[attribute_report_db]","coreDb"); f.append(F+"[attribute_event_id]","");
      ids.forEach(function(i){ f.append(F+"[attribute_event_id][]",String(i)); });
      ["agreement_contractor_id","agreement_id","organizer_id","event_state","event_start_date_start","event_start_date_end",
       "ticket_history_create_date_start","ticket_history_create_date_end","user_id","cash_office_id","yandex_user_id"]
        .forEach(function(k){ f.append(F+"[attribute_"+k+"]",""); });
      ["ACTIVE","CLOSED","SUSPENDED","CANCELLED"].forEach(function(s){ f.append(F+"[attribute_event_state][]",s); });
      [["agreement_include_sub","0"],["expand_external_event","0"],["expand_return","0"],["filter_none_zero","0"],
       ["ignore_discount_local","1"],["group_agreement","0"],["group_sector","0"],["format_id","11"]]
        .forEach(function(kv){ f.append(F+"[attribute_"+kv[0]+"]",kv[1]); });
      return fetch("/report/request/create?report_id=1",{method:"POST",credentials:"same-origin",body:f}).then(function(x){ return x.text(); });
    }).then(function(page){
      var m=page.match(/queue\/view\?id=(\d+)/); if(!m) throw new Error("касса не приняла запрос отчёта");
      var rid=m[1], n=0;
      function poll(){
        return get("/report/queue/status?id="+rid,true).then(function(r){
          var q=JSON.parse(r.text).data.request;
          if(q.status===3) return get(q.downloadUri).then(function(x){ return x.text; });
          if(q.status!==1 && q.status!==2) throw new Error("отчёт не сформирован: "+(q.statusName||q.status));
          if(++n>40) throw new Error("отчёт формируется слишком долго");
          say("Касса готовит отчёт… "+(n*2)+" с"); return sleep(2000).then(poll);
        });
      }
      return poll();
    });
  }
  /* разбор отчёта — то же, что parse_sales в yc/function/kassa.py */
  function parse(html){
    var doc=new DOMParser().parseFromString(html,"text/html"), out=[], cur=null;
    var EV=/Событие[^:]*:\s*(.+?) \/ (\d{4}-\d{2}-\d{2}) \/ (\d{1,2}:\d{2}) \/ [^/]* \/ ([^/]+?) \/ (\d+) \//, VEN=/Площадка:\s*([^/|]+)/;
    [].forEach.call(doc.querySelectorAll("tr"),function(tr){
      var cells=[].map.call(tr.querySelectorAll("td,th"),function(c){ return c.textContent.trim(); });
      var text=cells.join(" "), m=text.match(EV);
      if(m){ cur={name:m[1].trim(),date:m[2],time:m[3],state:m[4].trim(),id:parseInt(m[5],10),venue:""}; return; }
      var v=text.match(VEN);
      if(cur && v){ cur.venue=v[1].trim(); return; }
      if(cur && tr.classList.contains("report-data-total")){
        var n=cells.slice(1).map(function(x){ return /^-?\d+$/.test(x)?parseInt(x,10):0; });
        if(n.length>=18){ cur.quota=n[0]; cur.free=n[2]; cur.reserved=n[6]; cur.sold=n[8]; cur.returned=n[16];
          cur.acc=/Шукт/i.test(cur.venue)?"main":"butri"; out.push(cur); }
        cur=null;
      }
    });
    return out;
  }
  function getCode(force){
    var c=null; try{ c=localStorage.getItem(CODE_KEY); }catch(e){}
    if(!c || force){ c=prompt("Код организатора афиши (спросит один раз):",""); if(!c) return null; c=c.trim().toLowerCase();
      try{ localStorage.setItem(CODE_KEY,c); }catch(e){} }
    return c;
  }
  function push(evs, retry){
    var code=getCode(retry); if(!code) throw new Error("нужен код организатора");
    return fetch(API,{method:"POST",headers:{"Content-Type":"text/plain;charset=UTF-8"},body:JSON.stringify({op:"kassa_push",code:code,events:evs})})
      .then(function(r){ return r.json(); }).then(function(j){
        if(j.error==="bad_code" && !retry){ try{ localStorage.removeItem(CODE_KEY); }catch(e){} return push(evs,true); }
        if(!j.ok) throw new Error(j.error==="read_only"?"этот код только для просмотра — нужен код организатора":"сайт не принял данные ("+j.error+")");
        return j;
      });
  }

  say("Смотрю мероприятия в кабинете…");
  events().then(function(ids){
    if(!ids.length) throw new Error("в кабинете нет мероприятий");
    say("Заказываю отчёт по "+ids.length+" мероприятиям…");
    return report(ids);
  }).then(function(html){
    var evs=parse(html);
    if(!evs.length) throw new Error("не нашёл продажи в отчёте");
    say("Отправляю на афишу…");
    return push(evs).then(function(j){
      var top=evs.slice().sort(function(a,b){ return a.date.localeCompare(b.date); }).filter(function(e){ return e.sold>0; })
        .map(function(e){ return e.date.slice(8,10)+"."+e.date.slice(5,7)+" — "+e.sold; }).join(", ");
      done("Готово: "+j.events+" мероприятий, продано "+j.sold+" билетов."+(top?" По датам: "+top+".":"")+
        " Эта учётка — "+(evs[0].acc==="main"?"Шукты":"Бутри")+". Для второй учётки войдите в неё и нажмите закладку ещё раз.");
    });
  }).catch(function(e){
    var t=String(e && e.message || e);
    if(t==="login") fail("Похоже, вход в кабинет не выполнен. Войдите (с галочкой «я не робот») и нажмите закладку снова.");
    else if(t==="csrf") fail("Не открылась форма отчёта. Обновите страницу кабинета и нажмите закладку снова.");
    else fail("Не получилось: "+t);
  });
})();
