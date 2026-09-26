document.querySelector('#artifact-search')?.addEventListener('input',e=>{
  const query=e.target.value.trim().toLocaleLowerCase();
  document.querySelectorAll('.artifact-section').forEach(section=>{section.hidden=!section.textContent.toLocaleLowerCase().includes(query);});
});
