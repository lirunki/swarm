/* Browser-local tab order and server-owned cross-group shortcuts. No agent process actions. */
(function(root){
  'use strict';

  const MAX_ENTRIES=512;
  const EMPTY=()=>({version:1,order:[],starred:[]});
  const UUID=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const DEFINITE_FAILURES=new Set([400,401,403,404,422]);
  const validID=id=>typeof id==='string'&&id.length>0&&id.length<=512&&!/[\x00-\x1f\x7f]/.test(id);
  const validToken=value=>value==='browser'||(typeof value==='string'&&(/^(?:agent|agent-group):/.test(value))&&UUID.test(value.slice(value.indexOf(':')+1)));
  const copyProjection=value=>({revision:value.revision,starred:value.starred.slice()});

  function validProjection(value){
    return !!value&&typeof value==='object'&&!Array.isArray(value)&&Number.isInteger(value.revision)&&value.revision>=0&&
      Array.isArray(value.starred)&&value.starred.length<=MAX_ENTRIES&&value.starred.every(validToken)&&new Set(value.starred).size===value.starred.length;
  }
  function normalize(value){
    const result=EMPTY();
    if(!value||value.version!==1)return result;
    for(const field of ['order','starred'])if(Array.isArray(value[field]))result[field]=[...new Set(value[field].filter(validID))].slice(0,MAX_ENTRIES);
    return result;
  }
  function move(order,id,target,after=false){
    if(id===target||!order.includes(id)||!order.includes(target))return order.slice();
    const next=order.filter(value=>value!==id),index=next.indexOf(target)+(after?1:0);
    next.splice(index,0,id);return next;
  }
  function secureRequestId(){
    const shared=root.SwarmAgentMessages?.requestId;
    const value=typeof shared==='function'?shared():root.crypto?.randomUUID?.();
    if(!UUID.test(value||''))throw Error('A secure UUID request ID is unavailable');
    return value;
  }

  class TabPreferences{
    constructor({query=selector=>root.document.querySelector(selector),storage=()=>root.localStorage,onActivate=()=>{},
      api=async()=>{throw Error('Starred tabs are unavailable');},currentSwarm=()=> 'local',currentGeneration=()=>0,
      onChanged=async()=>{},onError=()=>{},create=tag=>root.document.createElement(tag),confirm=text=>root.confirm(text),
      requestTimeout=20000,setTimer=(callback,delay)=>root.setTimeout(callback,delay),clearTimer=timer=>root.clearTimeout(timer),
      requestId=secureRequestId}={}){
      Object.assign(this,{query,storage,onActivate,api,currentSwarm,currentGeneration,onChanged,onError,create,confirm,
        requestTimeout,setTimer,clearTimer,requestId});
      this.key=null;this.value=EMPTY();this.items=[];this.drag=null;this.menuSignature=null;
      this.source=currentSwarm();this.generation=currentGeneration();this.projection=null;this.projectionValid=false;
      this.availabilityError='Starred tabs are unavailable.';this.actionError='';this.busy=false;this.operation=0;this.uncertain=null;
      this.imported=false;this.lastReportedError='';
      this.tabs=query('#tabs');this.trigger=query('#starred-tabs-button');this.menu=query('#starred-tabs-menu');this.list=query('#starred-tabs-list');
      this.status=query('#starred-tabs-status');this.refreshButton=query('#starred-tabs-refresh');this.importButton=query('#starred-tabs-import');
      this.trigger?.addEventListener('click',()=>{
        if(this.menu?.open)this.menu.close();
        else{this.renderMenu();this.menu?.showModal();void this.refreshStars();}
      });
      query('#starred-tabs-close')?.addEventListener('click',()=>this.menu?.close());
      this.refreshButton?.addEventListener('click',()=>void this.refreshStars());
      this.importButton?.addEventListener('click',()=>void this.importStars());
      this.menu?.addEventListener('click',event=>{if(event.target===this.menu){const r=this.menu.getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)this.menu.close();}});
      this.tabs?.addEventListener('dragstart',event=>{
        const tab=event.target.closest?.('[data-agent-tab]');
        if(!tab||tab.hidden||event.target.closest?.('.tab-close,.tab-star')){event.preventDefault();return;}
        this.drag={id:tab.dataset.agentTab,key:this.key};tab.classList.add('tab-dragging');
        event.dataTransfer?.setData('text/plain',tab.dataset.agentTab);if(event.dataTransfer)event.dataTransfer.effectAllowed='move';
      });
      this.tabs?.addEventListener('dragover',event=>{const target=event.target.closest?.('[data-agent-tab]');if(this.drag?.key===this.key&&target&&!target.hidden){event.preventDefault();if(event.dataTransfer)event.dataTransfer.dropEffect='move';}});
      this.tabs?.addEventListener('drop',event=>{
        const target=event.target.closest?.('[data-agent-tab]');if(!this.drag||this.drag.key!==this.key||!target||target.hidden)return;
        event.preventDefault();const rect=target.getBoundingClientRect();this.reorder(this.drag.id,target.dataset.agentTab,event.clientX>rect.x+rect.width/2);this.endDrag();
      });
      this.tabs?.addEventListener('dragend',()=>this.endDrag());
      this.tabs?.addEventListener('keydown',event=>{
        if(!event.altKey||!event.shiftKey||!['ArrowLeft','ArrowRight'].includes(event.key))return;
        const tab=event.target.closest?.('[data-agent-tab]');if(!tab||tab.hidden)return;
        const visible=this.ordered(this.items.filter(item=>item.kind==='agent'&&item.element&&!item.element.hidden));
        const index=visible.findIndex(item=>item.agentId===tab.dataset.agentTab),step=event.key==='ArrowRight'?1:-1,target=visible[index+step];
        if(target){event.preventDefault();this.reorder(tab.dataset.agentTab,target.agentId,step>0);tab.querySelector('.tab')?.focus();}
      });
      this.renderControls();
    }

    endpoint(source){return source==='local'?'/api/starred-tabs':`/api/federation/${encodeURIComponent(source)}/proxy/api/starred-tabs`;}
    inContext(source=this.source,generation=this.generation){return source===this.currentSwarm()&&generation===this.currentGeneration();}
    stars(){return this.projection?.starred||[];}
    mutationsDisabled(){return !this.projectionValid||!!this.availabilityError||this.busy||!!this.uncertain;}
    migrationTokens(){return [...new Set(this.value.starred.filter(validToken))];}
    markerKey(){return this.key?`${this.key}.stars-imported`:null;}

    endDrag(){for(const item of this.items)item.element?.classList.remove('tab-dragging');this.drag=null;}
    load(key){
      if(key===this.key)return;
      this.endDrag();this.menu?.close();this.menuSignature=null;this.key=key;this.value=EMPTY();this.imported=false;
      try{
        const store=this.storage(),saved=store.getItem(key);if(saved)this.value=normalize(JSON.parse(saved));
        this.imported=store.getItem(this.markerKey())==='true';
      }catch{this.onError('Tab preferences could not be loaded; this view uses defaults.');}
    }
    save(next){
      try{this.storage().setItem(this.key,JSON.stringify(next));this.value=next;return true;}
      catch{this.onError('Tab preferences could not be saved.');return false;}
    }
    markImported(){
      try{this.storage().setItem(this.markerKey(),'true');this.imported=true;}catch{/* Keep the explicit import available when storage is blocked. */}
    }
    ordered(items){const positions=new Map(this.value.order.map((id,index)=>[id,index]));return items.slice().sort((a,b)=>(positions.get(a.id)??1e9)-(positions.get(b.id)??1e9));}
    reorder(id,target,after){
      const candidates=this.items.filter(item=>item.kind==='agent'&&item.element&&!item.element.hidden);
      if(!candidates.some(item=>item.agentId===id)||!candidates.some(item=>item.agentId===target))return;
      const all=this.ordered(this.items.filter(item=>item.kind==='agent'));
      const tokens=all.map(item=>item.id),next=move(tokens,'agent:'+id,'agent:'+target,after);
      if(this.save({...this.value,order:next}))this.applyOrder();
    }
    applyOrder(){
      // Reorder only terminal slots: Dashboard, active folder and Browser anchors stay put.
      const desired=this.ordered(this.items.filter(item=>item.kind==='agent'&&item.element));
      if(!desired.length)return;
      const elements=new Set(desired.map(item=>item.element));
      const slots=[...this.tabs.children].filter(element=>elements.has(element));
      if(slots.length===desired.length&&slots.every((element,index)=>element===desired[index].element))return;
      const focused=this.tabs.ownerDocument.activeElement,doc=this.tabs.ownerDocument;
      const markers=slots.map(element=>{const marker=doc.createComment('terminal-tab');this.tabs.insertBefore(marker,element);element.remove();return marker;});
      desired.forEach((item,index)=>{if(markers[index])markers[index].replaceWith(item.element);});
      if(focused?.isConnected)focused.focus({preventScroll:true});
    }

    sync({key,items,starred,error}={}){
      this.load(key);this.items=Array.isArray(items)?items:[];
      const source=this.currentSwarm(),generation=this.currentGeneration(),contextChanged=source!==this.source||generation!==this.generation;
      let next=validProjection(starred)?copyProjection(starred):null;
      const staleProjection=!contextChanged&&next&&this.projection&&next.revision<this.projection.revision;
      if(staleProjection)next=null;
      if(contextChanged){
        this.source=source;this.generation=generation;this.operation+=1;this.busy=false;this.uncertain=null;this.actionError='';
        this.projection=null;this.projectionValid=false;
      }
      const malformed=!staleProjection&&starred!==undefined&&starred!==null&&!next;
      if(next){
        const previousRevision=this.projection?.revision,hadUncertain=!!this.uncertain;
        this.projection=next;this.projectionValid=true;
        if(this.uncertain&&next.revision>(this.uncertain.revision??-1))this.resolveUncertain(next,false);
        if(!hadUncertain&&previousRevision!==undefined&&next.revision>previousRevision)this.actionError='';
      }else if(!staleProjection)this.projectionValid=false;
      if(!staleProjection)this.availabilityError=error?String(error.message||error):malformed?'Current starred tabs state is invalid.':next?null:'Starred tabs are not supported by this server.';
      this.menuSignature=null;this.renderStars();if(!this.drag)this.applyOrder();this.renderControls();if(this.menu?.open)this.renderMenu();
    }

    async bounded(path,method,body,label){
      let timer;
      try{
        const request=Promise.resolve(this.api(path,method,body));
        const timeout=new Promise((resolve,reject)=>{timer=this.setTimer(()=>reject(Error(`${label} timed out after ${this.requestTimeout}ms`)),this.requestTimeout);});
        return await Promise.race([request,timeout]);
      }finally{if(timer!==undefined)this.clearTimer(timer);}
    }
    report(message){
      this.actionError=String(message);
      if(this.actionError!==this.lastReportedError){this.lastReportedError=this.actionError;this.onError(this.actionError);}
      this.renderControls();this.renderStars();this.renderMenu();
    }
    acceptProjection(value){
      if(!validProjection(value))return false;
      if(this.projection&&value.revision<this.projection.revision)return false;
      this.projection=copyProjection(value);this.projectionValid=true;this.availabilityError=null;this.menuSignature=null;return true;
    }
    async changed(snapshot,projection,metadata){
      if(!this.inContext(snapshot.source,snapshot.generation)||snapshot.operation!==this.operation)return false;
      await this.onChanged(copyProjection(projection),{...metadata,source:snapshot.source,generation:snapshot.generation});
      return this.inContext(snapshot.source,snapshot.generation)&&snapshot.operation===this.operation;
    }
    desiredMatches(projection,pending){
      if(pending.action==='import')return pending.tokens.every(token=>projection.starred.includes(token));
      return projection.starred.includes(pending.id)===pending.starred;
    }
    resolveUncertain(projection,explicit){
      if(!this.uncertain)return;
      const pending=this.uncertain;
      if(!explicit&&projection.revision<=pending.revision)return;
      this.uncertain=null;
      const matched=this.desiredMatches(projection,pending);
      if(matched&&pending.action==='import')this.markImported();
      this.actionError=matched?'':`The request outcome is now known. Review the current starred tabs and try again if needed. Request ID ${pending.requestId}.`;
    }

    async refreshStars(){
      const source=this.currentSwarm(),generation=this.currentGeneration();
      if(!this.inContext(source,generation))return false;
      const operation=++this.operation,snapshot={source,generation,operation};this.busy=true;this.renderControls();this.renderStars();
      try{
        const value=await this.bounded(this.endpoint(source),undefined,undefined,'Starred tabs refresh');
        if(!this.inContext(source,generation)||operation!==this.operation)return false;
        if(!validProjection(value))throw Error('Owning server returned an invalid starred tabs projection');
        if(this.projection&&value.revision<this.projection.revision){this.actionError='';return false;}
        this.acceptProjection(value);this.resolveUncertain(this.projection,true);
        this.menuSignature=null;this.renderStars();this.renderMenu();this.renderControls();
        await this.changed(snapshot,this.projection,{operation:'refresh'});return true;
      }catch(error){
        if(!this.inContext(source,generation)||operation!==this.operation)return false;
        this.availabilityError=String(error?.message||error);this.report(`Starred tabs could not be refreshed. ${this.availabilityError}`);return false;
      }finally{
        if(this.inContext(source,generation)&&operation===this.operation){this.busy=false;this.renderControls();this.renderStars();this.renderMenu();}
      }
    }

    async mutate(action,{id=null,tokens=null}={}){
      if(this.mutationsDisabled()||!this.projection)return false;
      if(action!=='import'&&!validToken(id))return false;
      const cleanTokens=action==='import'?[...new Set((tokens||[]).filter(validToken))]:null;
      if(action==='import'&&!cleanTokens.length)return false;
      const source=this.currentSwarm(),generation=this.currentGeneration();
      if(!this.inContext(source,generation))return false;
      const known=copyProjection(this.projection),operation=++this.operation,snapshot={source,generation,operation};
      let requestId;
      try{requestId=this.requestId();if(!UUID.test(requestId||''))throw Error('Invalid request ID');}
      catch(error){this.report(error?.message||error);return false;}
      const desired=action==='import'?{action,tokens:cleanTokens,requestId,revision:known.revision}:
        {action,id,starred:action==='star',requestId,revision:known.revision};
      const body=action==='import'?{action,revision:known.revision,request_id:requestId,starred:cleanTokens}:
        {action,revision:known.revision,request_id:requestId,tab_id:id};
      this.busy=true;this.actionError='';this.renderStars();this.renderMenu();this.renderControls();
      try{
        const value=await this.bounded(this.endpoint(source),'POST',body,'Starred tabs update');
        if(!this.inContext(source,generation)||operation!==this.operation)return false;
        if(!validProjection(value)||!this.desiredMatches(value,desired))throw Error('Owning server returned an invalid starred tabs update acknowledgement');
        if(!this.acceptProjection(value))return false;
        this.actionError='';this.lastReportedError='';
        if(action==='import')this.markImported();
        this.renderStars();this.renderMenu();this.renderControls();
        await this.changed(snapshot,this.projection,{operation:action,requestId});return true;
      }catch(error){
        if(!this.inContext(source,generation)||operation!==this.operation)return false;
        if(error?.status===409){
          this.actionError='Starred tabs changed on the server. Refreshing before another attempt.';
          this.busy=false;this.renderControls();this.renderStars();this.renderMenu();
          const refreshed=await this.refreshStars();
          if(refreshed&&this.inContext(source,generation))this.report('Starred tabs changed on the server. Review the refreshed list and click again.');
        }else if(DEFINITE_FAILURES.has(error?.status)){
          this.report(`Starred tabs update failed. Request ID ${requestId}. ${error?.message||error}`);
        }else{
          this.uncertain=desired;
          this.report(`Starred tabs update outcome is uncertain. Do not retry blindly. Request ID ${requestId}. ${error?.message||error}`);
        }
        return false;
      }finally{
        if(this.inContext(source,generation)&&operation===this.operation){this.busy=false;this.renderControls();this.renderStars();this.renderMenu();}
      }
    }

    async toggle(id){
      if(!validToken(id)||this.mutationsDisabled())return false;
      const itemKnown=this.items.some(item=>item.id===id),already=this.stars().includes(id);
      if(!already&&!itemKnown)return false;
      return this.mutate(already?'unstar':'star',{id});
    }
    async importStars(){
      const tokens=this.migrationTokens();
      if(!tokens.length||this.imported||this.mutationsDisabled())return false;
      if(!(await this.confirm(`Import ${tokens.length} local starred tab${tokens.length===1?'':'s'} and merge with the shared list?`)))return false;
      if(!this.inContext()||this.mutationsDisabled())return false;
      return this.mutate('import',{tokens});
    }

    renderControls(){
      const tokens=this.migrationTokens(),canImport=tokens.length>0&&!this.imported;
      if(this.importButton){this.importButton.hidden=!canImport;this.importButton.disabled=!canImport||this.mutationsDisabled();}
      if(this.refreshButton){this.refreshButton.disabled=this.busy;this.refreshButton.textContent=this.busy?'Refreshing…':'Refresh';}
      if(this.status){
        if(this.uncertain)this.status.textContent=this.actionError||'A starred-tabs update has an uncertain outcome. Refresh before changing stars.';
        else if(this.actionError)this.status.textContent=this.actionError;
        else if(this.availabilityError)this.status.textContent=this.availabilityError;
        else this.status.textContent='Starred tabs are shared by browsers connected to this owning Swarm.';
      }
    }
    renderStars(){
      const stars=this.stars(),disabled=this.mutationsDisabled();
      for(const item of this.items){
        if(!item.element)continue;
        if(item.kind==='agent'){item.element.dataset.agentTab=item.agentId;item.element.draggable=true;item.element.title='Drag to reorder; Alt+Shift+Left/Right also moves this tab';}
        let star=item.element.querySelector('.tab-star');
        if(!star){
          star=this.create('button');star.type='button';star.className='tab-star';
          star.addEventListener('click',event=>{event.stopPropagation();void this.toggle(item.id);});
          const anchor=item.kind==='agent'?item.element.querySelector('.tab'):item.element.querySelector('.tab-close');item.element.insertBefore(star,anchor);
        }
        const starred=stars.includes(item.id);star.textContent=starred?'★':item.kind==='agent'?'›_':'☆';star.disabled=disabled;
        star.setAttribute('aria-pressed',String(starred));star.setAttribute('aria-label',`${starred?'Unstar':'Star'} ${item.name}`);star.title=disabled?'Shared starred tabs are currently unavailable.':`${starred?'Remove from':'Add to'} cross-group starred tabs`;
      }
      if(this.trigger){this.trigger.textContent=`★ ${stars.length}`;this.trigger.setAttribute('aria-label',`Starred tabs (${stars.length})`);}
    }
    renderMenu(){
      if(!this.list)return;
      const stars=this.stars(),signature=JSON.stringify([stars,this.items.map(item=>[item.id,item.name,item.group,item.stopped]),this.mutationsDisabled(),this.actionError,this.availabilityError]);
      if(this.menuSignature===signature)return;this.menuSignature=signature;this.list.replaceChildren();const byID=new Map(this.items.map(item=>[item.id,item]));
      if(!stars.length){const empty=this.create('p');empty.className='hint';empty.textContent='Star a tab with ☆ to access it from any group.';this.list.append(empty);}
      for(const id of stars){
        const item=byID.get(id),row=this.create('div');row.className='starred-tab-row';const open=this.create('button');open.type='button';open.className='starred-tab-open';open.disabled=!item;
        open.textContent=item?`${item.name} · ${item.group||'Dashboard'}${item.stopped?' · stopped':''}`:'Unavailable tab';
        open.addEventListener('click',()=>{this.menu?.close();this.onActivate(id);});
        const remove=this.create('button');remove.type='button';remove.className='small';remove.textContent='★';remove.disabled=this.mutationsDisabled();
        remove.setAttribute('aria-label',`Unstar ${item?.name||'unavailable tab'}`);remove.addEventListener('click',()=>void this.toggle(id));row.append(open,remove);this.list.append(row);
      }
      this.renderControls();
    }
  }

  const api={normalize,move,validToken,validProjection,TabPreferences};
  if(typeof module==='object'&&module.exports)module.exports=api;else root.SwarmTabPreferences=api;
})(globalThis);
