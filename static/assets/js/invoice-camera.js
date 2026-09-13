(function(){
  function init(){
    const button=document.getElementById('invoice-camera-button');
    const input=document.getElementById('invoice-camera-input');
    if(!button||!input||button.dataset.cameraInstalled)return;
    button.dataset.cameraInstalled='true';
    const style=document.createElement('style');
    style.textContent='.invoice-camera-modal{position:fixed;inset:0;z-index:1080;display:none;align-items:center;justify-content:center;padding:1rem;background:rgba(15,23,42,.72)}.invoice-camera-modal.is-open{display:flex}.invoice-camera-dialog{width:min(720px,100%);max-height:calc(100vh - 2rem);overflow:auto;border-radius:20px;background:#fff;box-shadow:0 24px 80px rgba(15,23,42,.28)}.invoice-camera-preview{background:#0f172a;aspect-ratio:4/3;overflow:hidden}.invoice-camera-preview video{display:block;width:100%;height:100%;object-fit:cover}.invoice-camera-actions{display:flex;justify-content:flex-end;gap:.6rem;flex-wrap:wrap;padding:1rem}.invoice-camera-error{margin:0 1rem 1rem}';
    document.head.appendChild(style);
    const modal=document.createElement('div');
    modal.id='invoice-camera-modal';
    modal.className='invoice-camera-modal';
    modal.setAttribute('role','dialog');
    modal.setAttribute('aria-modal','true');
    modal.innerHTML='<div class="invoice-camera-dialog"><div class="p-3 d-flex justify-content-between align-items-center gap-2"><div><h2 class="h5 fw-bold mb-1">Fotografiar factura</h2><div class="small text-muted">Acomodá la factura dentro del cuadro y sacá la foto.</div></div><button id="invoice-camera-close" class="btn btn-sm btn-outline-secondary" type="button">Cerrar</button></div><div class="invoice-camera-preview"><video id="invoice-camera-video" autoplay playsinline muted></video></div><div id="invoice-camera-error" class="invoice-camera-error small text-danger" role="alert"></div><div class="invoice-camera-actions"><button id="invoice-camera-cancel" class="btn btn-outline-secondary" type="button">Cancelar</button><button id="invoice-camera-capture" class="btn btn-primary" type="button"><i class="bi bi-camera me-1"></i>Capturar foto</button></div></div>';
    document.body.appendChild(modal);
    const video=modal.querySelector('#invoice-camera-video');
    const capture=modal.querySelector('#invoice-camera-capture');
    const close=modal.querySelector('#invoice-camera-close');
    const cancel=modal.querySelector('#invoice-camera-cancel');
    const errorBox=modal.querySelector('#invoice-camera-error');
    let stream=null;
    function stop(){if(stream){stream.getTracks().forEach(t=>t.stop());stream=null}video.srcObject=null;modal.classList.remove('is-open');document.body.classList.remove('overflow-hidden')}
    function fallback(message){stop();if(message)errorBox.textContent=message;input.click()}
    async function openCamera(){
      errorBox.textContent='';
      if(!navigator.mediaDevices||!navigator.mediaDevices.getUserMedia){fallback('Tu navegador no permite acceso directo a la cámara. Se abrió el selector de imágenes como alternativa.');return}
      try{stream=await navigator.mediaDevices.getUserMedia({video:{facingMode:{ideal:'environment'},width:{ideal:1920},height:{ideal:1080}},audio:false});video.srcObject=stream;modal.classList.add('is-open');document.body.classList.add('overflow-hidden');await video.play().catch(()=>{})}
      catch(e){console.warn('No se pudo abrir la cámara de factura',e);fallback('No se pudo acceder a la cámara. Revisá el permiso del navegador; mientras tanto podés seleccionar una foto.')}
    }
    function capturePhoto(){
      if(!video.videoWidth||!video.videoHeight){errorBox.textContent='Esperá un momento hasta que la cámara esté lista.';return}
      const canvas=document.createElement('canvas');canvas.width=video.videoWidth;canvas.height=video.videoHeight;canvas.getContext('2d').drawImage(video,0,0,canvas.width,canvas.height);
      canvas.toBlob(blob=>{if(!blob){errorBox.textContent='No se pudo capturar la foto. Intentá nuevamente.';return}try{const dt=new DataTransfer();const file=new File([blob],`factura-camera-${Date.now()}.jpg`,{type:'image/jpeg',lastModified:Date.now()});dt.items.add(file);input.files=dt.files;input.dispatchEvent(new Event('change',{bubbles:true}));stop()}catch(e){console.warn('No se pudo transferir la captura',e);errorBox.textContent='No se pudo preparar la foto para la IA. Intentá nuevamente.'}},'image/jpeg',.92)
    }
    button.addEventListener('click',openCamera);
    capture.addEventListener('click',capturePhoto);
    close.addEventListener('click',stop);
    cancel.addEventListener('click',stop);
    modal.addEventListener('click',e=>{if(e.target===modal)stop()});
    document.addEventListener('keydown',e=>{if(e.key==='Escape'&&modal.classList.contains('is-open'))stop()});
    window.addEventListener('pagehide',stop);
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init,{once:true});else init();
})();