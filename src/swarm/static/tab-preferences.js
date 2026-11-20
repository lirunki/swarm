/* Browser-local tab order and cross-group shortcuts. No agent process actions. */
(function(root){
  'use strict';
  const EMPTY=()=>({version:1,order:[],starred:[]});
  const validID=id=>typeof id==='string'&&id.length>0&&id.length<=512&&!/[\x00-\x1f\x7f]/.test(id);
  function normalize(value){
    const result=EMPTY();
    if(!value||value.version!==1)return result;
    for(const field of ['order','starred'])if(Array.isArray(value[field]))result[field]=[...new Set(value[field].filter(validID))].slice(0,512);
    return result;
  }
  function move(order,id,target,after=false){
    if(id===target||!order.includes(id)||!order.includes(target))return order.slice();
    const next=order.filter(value=>value!==id),index=next.indexOf(target)+(after?1:0);
    next.splice(index,0,id);return next;
  }
  class TabPreferences{
    constructor({query=selector=>document.querySelector(selector),storage=()=>localStorage,onActivate=()=>{},onChanged=()=>{},onError=()=>{},create=tag=>document.createElement(tag)}={}){
      Object.assign(this,{query,storage,onActivate,onChanged,onError,create});this.key=null;this.value=EMPTY();this.items=[];this.drag=null;this.menuSignature=null;
      this.tabs=query('#tabs');this.trigger=query('#starred-tabs-button');this.menu=query('#starred-tabs-menu');this.list=query('#starred-tabs-list');
      this.trigger?.addEventListener('click',()=>{if(this.menu.open)this.menu.close();else{this.renderMenu();this.menu.showModal();}});
      query('#starred-tabs-close')?.addEventListener('click',()=>this.menu.close());
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
    }
    endDrag(){for(const item of this.items)item.element?.classList.remove('tab-dragging');this.drag=null;}
    load(key){
      if(key===this.key)return;this.endDrag();this.menu?.close();this.menuSignature=null;this.key=key;this.value=EMPTY();
      try{const saved=this.storage().getItem(key);if(saved)this.value=normalize(JSON.parse(saved));}catch{this.onError('Tab preferences could not be loaded; this view uses defaults.');}
    }
    save(next){
      try{this.storage().setItem(this.key,JSON.stringify(next));this.value=next;return true;}
      catch{this.onError('Tab preferences could not be saved.');return false;}
    }
    ordered(items){const positions=new Map(this.value.order.map((id,index)=>[id,index]));return items.slice().sort((a,b)=>(positions.get(a.id)??1e9)-(positions.get(b.id)??1e9));}
    reorder(id,target,after){
      const candidates=this.items.filter(item=>item.kind==='agent'&&item.element&&!item.element.hidden);
      if(!candidates.some(item=>item.agentId===id)||!candidates.some(item=>item.agentId===target))return;
      const all=this.ordered(this.items.filter(item=>item.kind==='agent'));
      const tokens=all.map(item=>item.id),next=move(tokens,'agent:'+id,'agent:'+target,after);
      if(this.save({...this.value,order:next})){this.applyOrder();this.onChanged();}
    }
    toggle(id){
      const next=this.value.starred.includes(id)?this.value.starred.filter(value=>value!==id):[...this.value.starred,id];
      if(this.save({...this.value,starred:next})){this.renderStars();this.renderMenu();this.onChanged();}
    }
    applyOrder(){
      // Reorder only terminal slots: Dashboard, active folder and Browser anchors stay put.
      const desired=this.ordered(this.items.filter(item=>item.kind==='agent'&&item.element));
      if(!desired.length)return;
      const elements=new Set(desired.map(item=>item.element));
      const slots=[...this.tabs.children].filter(element=>elements.has(element));
      if(slots.length===desired.length && slots.every((element,index)=>element===desired[index].element))return;
      const focused=this.tabs.ownerDocument.activeElement;
      const doc=this.tabs.ownerDocument;const markers=slots.map(element=>{const marker=doc.createComment('terminal-tab');this.tabs.insertBefore(marker,element);element.remove();return marker;});
      desired.forEach((item,index)=>{if(markers[index])markers[index].replaceWith(item.element);});
      if(focused?.isConnected)focused.focus({preventScroll:true});
    }
    sync({key,items}){
      this.load(key);this.items=items;this.renderStars();if(!this.drag)this.applyOrder();if(this.menu?.open)this.renderMenu();
    }
    renderStars(){
      for(const item of this.items){
        if(!item.element)continue;
        if(item.kind==='agent'){item.element.dataset.agentTab=item.agentId;item.element.draggable=true;item.element.title='Drag to reorder; Alt+Shift+Left/Right also moves this tab';}
        let star=item.element.querySelector('.tab-star');
        if(!star){star=this.create('button');star.type='button';star.className='tab-star';star.addEventListener('click',event=>{event.stopPropagation();this.toggle(item.id);});const anchor=item.kind==='agent'?item.element.querySelector('.tab'):item.element.querySelector('.tab-close');item.element.insertBefore(star,anchor);}
        const starred=this.value.starred.includes(item.id);star.textContent=starred?'★':item.kind==='agent'?'›_':'☆';star.setAttribute('aria-pressed',String(starred));star.setAttribute('aria-label',`${starred?'Unstar':'Star'} ${item.name}`);star.title=`${starred?'Remove from':'Add to'} cross-group starred tabs`;
      }
      if(this.trigger){this.trigger.textContent=`★ ${this.value.starred.length}`;this.trigger.setAttribute('aria-label',`Starred tabs (${this.value.starred.length})`);}
    }
    renderMenu(){
      if(!this.list)return;
      const signature=JSON.stringify([this.value.starred,this.items.map(item=>[item.id,item.name,item.group,item.stopped])]);
      if(this.menuSignature===signature)return;this.menuSignature=signature;this.list.replaceChildren();const byID=new Map(this.items.map(item=>[item.id,item]));
      if(!this.value.starred.length){const empty=this.create('p');empty.className='hint';empty.textContent='Star a tab with ☆ to access it from any group.';this.list.append(empty);}
      for(const id of this.value.starred){const item=byID.get(id),row=this.create('div');row.className='starred-tab-row';const open=this.create('button');open.type='button';open.className='starred-tab-open';open.disabled=!item;open.textContent=item?`${item.name} · ${item.group||'Dashboard'}${item.stopped?' · stopped':''}`:'Unavailable tab';open.addEventListener('click',()=>{this.menu.close();this.onActivate(id);});const remove=this.create('button');remove.type='button';remove.className='small';remove.textContent='★';remove.setAttribute('aria-label',`Unstar ${item?.name||'unavailable tab'}`);remove.addEventListener('click',()=>this.toggle(id));row.append(open,remove);this.list.append(row);}
    }
  }
  const api={normalize,move,TabPreferences};if(typeof module==='object'&&module.exports)module.exports=api;else root.SwarmTabPreferences=api;
})(globalThis);
