const form = document.querySelector('#reading-upload');
form.addEventListener('submit', event => {
  event.preventDefault();
  const button = form.querySelector('button[type="submit"]');
  const progress = document.querySelector('#upload-progress');
  const status = document.querySelector('#upload-status');
  const xhr = new XMLHttpRequest();
  button.disabled = true;
  progress.hidden = false;
  status.textContent = '正在上传，请保持此页面打开。';
  xhr.open('POST', form.action || location.href);
  xhr.upload.onprogress = e => {
    if(e.lengthComputable) {
      progress.value = Math.round(e.loaded / e.total * 100);
      status.textContent = progress.value === 100 ? '文件传输完成，正在保存…' : `正在上传 ${progress.value}%`;
    }
  };
  xhr.onerror = () => {button.disabled=false; status.textContent='网络中断，未确认保存成功。可重试上传；已保存的相同文件会提示重复。';};
  xhr.onload = () => {
    button.disabled = false;
    if(xhr.status === 200 && xhr.responseURL !== location.href) {location.assign(xhr.responseURL); return;}
    // Display server validation text without injecting another document or losing the file input.
    const doc = new DOMParser().parseFromString(xhr.responseText, 'text/html');
    const errors = [...doc.querySelectorAll('.errorlist')].map(x=>x.textContent.trim()).join(' ');
    status.textContent = errors || `上传未完成（${xhr.status}）。请检查登录状态、文件大小后重试。`;
  };
  xhr.send(new FormData(form));
});
