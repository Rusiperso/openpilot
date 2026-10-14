"use strict";

// The device uploads its own files. Do not fetch /data/... into the browser
// or post each file back to an endpoint that already sends the whole segment.
async function uploadSelectedG4Segments() {
  const selected = [...new Set(Array.from(document.querySelectorAll('.segment-checkbox:checked'), input => input.value))];
  if (!selected.length) {
    showUploadStatus('전송할 구간을 선택하세요.', 'warning');
    return;
  }
  const buttons = Array.from(document.querySelectorAll('#g4UploadButton, button[onclick*="uploadSelectedSegments"]'));
  if (buttons.some(button => button.disabled)) return;
  document.getElementById('video')?.pause();
  buttons.forEach(button => { button.disabled = true; });
  const status = document.getElementById('uploadStatus');
  status.replaceChildren();
  const progress = document.createElement('div');
  progress.className = 'alert alert-info';
  status.append(progress);
  let completed = 0;
  let savedPath = '';
  try {
    for (const name of selected) {
      const separator = name.lastIndexOf('--');
      const route = name.slice(0, separator);
      const segment = name.slice(separator + 2);
      if (separator < 1 || !/^\d+$/.test(segment)) throw new Error('잘못된 로그 구간입니다.');
      progress.textContent = `G4 서버 전송 중 ${completed}/${selected.length}: ${name}`;
      const response = await fetch(`/footage/full/upload_g4/${encodeURIComponent(route)}/${encodeURIComponent(segment)}`, {
        method: 'POST',
      });
      const result = await response.json();
      if (!response.ok || result.ok !== true) throw new Error(result.error || `HTTP ${response.status}`);
      completed += 1;
      savedPath = result.remoteBasePath || savedPath;
    }
    progress.className = 'alert alert-success';
    progress.textContent = `G4 서버 저장 완료 ${completed}/${selected.length} · ${savedPath}`;
  } catch (error) {
    progress.className = 'alert alert-danger';
    progress.textContent = `G4 전송 실패 (${completed}/${selected.length} 완료): ${error.message || error}`;
  } finally {
    buttons.forEach(button => { button.disabled = false; });
  }
}
