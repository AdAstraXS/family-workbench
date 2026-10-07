"""Describe saved work without triggering downloads, retries or paid calls."""


def program_progress(entry):
    revision = entry.current_revision
    completed = []
    if entry.audio_file:
        completed.append('已保存音频（有有效期）')
    if revision:
        completed.append('原文／文字稿已保存')
        done = revision.chunks.filter(status='success').count()
        if done:
            completed.append(f'已保存 {done} 段 AI 整理结果')
    if revision and revision.summary_complete:
        stage, next_step = '已完成', '可阅读原文与整理结果。'
    elif entry.state == 'uncertain':
        stage, next_step = '转写提交结果待核对', '先核对服务商任务，避免重复提交及重复计费。'
    elif revision:
        stage, next_step = 'AI 整理', '继续处理会复用原文与已成功的段落；剩余段落仍按原授权和预算执行。'
    elif entry.task_id:
        stage, next_step = '音频转写', '先查询已有转写任务；原页面会根据任务结果决定能否重试。'
    elif entry.audio_file:
        stage, next_step = '音频上传／转写提交', '原页面会检查缓存有效期、转写授权和预算后继续。'
    else:
        stage, next_step = '获取／解析原文', '检查来源访问情况后，在原页面继续处理。尚无已保存文字稿。'
    return {'stage': stage, 'completed': completed, 'next_step': next_step}
