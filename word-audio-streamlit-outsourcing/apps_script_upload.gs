const DEFAULT_TARGET_FOLDER_ID = '1rrpaErhjoSICF5NvhfHCArYUHmpF5QBW';

function doPost(e) {
  try {
    const body = JSON.parse((e && e.postData && e.postData.contents) || '{}');
    const props = PropertiesService.getScriptProperties();
    const expectedToken = props.getProperty('UPLOAD_TOKEN') || props.getProperty('GOOGLE_APPS_SCRIPT_TOKEN') || '';

    if (expectedToken && body.token !== expectedToken) {
      return jsonResponse({ ok: false, error: 'invalid token' });
    }

    const fileName = body.file_name || body.fileName || 'audio_upload.zip';
    const mimeType = body.mime_type || body.mimeType || 'application/octet-stream';
    const contentB64 = body.content_b64 || body.contentB64 || '';
    if (!contentB64) {
      return jsonResponse({ ok: false, error: 'missing content_b64' });
    }

    const targetFolderId =
      body.folder_id ||
      body.folderId ||
      body.target_folder_id ||
      props.getProperty('TARGET_FOLDER_ID') ||
      props.getProperty('GOOGLE_DRIVE_FOLDER_ID') ||
      DEFAULT_TARGET_FOLDER_ID;

    const bytes = Utilities.base64Decode(contentB64);
    const blob = Utilities.newBlob(bytes, mimeType, fileName);
    const folder = DriveApp.getFolderById(targetFolderId);
    const file = folder.createFile(blob).setName(fileName);

    return jsonResponse({
      ok: true,
      id: file.getId(),
      url: file.getUrl(),
      webViewLink: file.getUrl(),
      folder_id: targetFolderId
    });
  } catch (err) {
    return jsonResponse({ ok: false, error: String(err && err.message ? err.message : err) });
  }
}

function jsonResponse(payload) {
  return ContentService
    .createTextOutput(JSON.stringify(payload))
    .setMimeType(ContentService.MimeType.JSON);
}
