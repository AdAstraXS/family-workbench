# 阅读组件锁定版本

- foliate-js: MIT, upstream commit `78914aef4466eb960965702401634c2cb348e9b1`.
  https://github.com/johnfactotum/foliate-js
  此处保留根级 JS 模块和 LICENSE。生产入口只打开服务端生成的 EPUB 对象，不调用原始文件解析入口。
  动态分支中的 MOBI/ZIP/实验 PDF 依赖不在本阶段接入。
- PDF.js: Apache-2.0, npm `pdfjs-dist@6.3.289`.
  https://github.com/mozilla/pdf.js
  保留两个压缩 ESM 文件、cmaps、standard_fonts、wasm 及各目录声明。
- `../reader.css` 中 `.textLayer` 的必要排版规则参考同版本 `web/pdf_viewer.css`（Apache-2.0）。
- 仅清理少量上游文件的行尾空格及末尾空行，组件逻辑与许可证文本未改动。

完整下载来源、包哈希和实测范围见 `experiments/reading/README.md` 与
`docs/reading-v1-research-and-spike.md`。升级前验证保存位置、文字选择和安全净化后的样本，不使用浮动 CDN。
