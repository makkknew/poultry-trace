import { readFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import ExcelJS from 'exceljs'
import PDFDocument from 'pdfkit'

export function cellValue(value: unknown) {
  const text = value === null || value === undefined ? '' : typeof value === 'object' ? JSON.stringify(value) : String(value)
  return /^[\s]*[=+\-@\t\r]/.test(text) && typeof value !== 'number' ? `'${text}` : text
}

export async function exportReport(records: object[], reportType: string, format: 'csv' | 'xlsx' | 'pdf') {
  const headers = records.length ? Object.keys(records[0]) : ['id']
  const rows = records.map(record => headers.map(key => cellValue((record as Record<string, unknown>)[key])))
  const disposition = `attachment; filename="${reportType}.${format}"`
  if (format === 'csv') {
    const quote = (value: string) => `"${value.replaceAll('"', '""')}"`
    return new Response('\uFEFF' + [headers, ...rows].map(row => row.map(quote).join(',')).join('\r\n'), { headers: { 'content-type': 'text/csv; charset=utf-8', 'content-disposition': disposition } })
  }
  if (format === 'xlsx') {
    const workbook = new ExcelJS.Workbook()
    const worksheet = workbook.addWorksheet(reportType)
    worksheet.addRow(headers)
    worksheet.addRows(rows)
    worksheet.getRow(1).font = { bold: true }
    const buffer = await workbook.xlsx.writeBuffer()
    return new Response(new Uint8Array(buffer), { headers: { 'content-type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'content-disposition': disposition } })
  }
  const font = await readFile(resolve('netlify/assets/DejaVuSans.ttf'))
  const document = new PDFDocument({ size: 'A4', margin: 36, font })
  const chunks: Buffer[] = []
  const finished = new Promise<Buffer>((resolveBuffer, reject) => {
    document.on('data', chunk => chunks.push(chunk))
    document.on('end', () => resolveBuffer(Buffer.concat(chunks)))
    document.on('error', reject)
  })
  document.fontSize(16).text(`Poultry Trace — ${reportType}`).moveDown()
  if (!rows.length) document.fontSize(11).text('Нет записей для экспорта')
  for (const row of rows) {
    document.fontSize(9).text(headers.map((header, index) => `${header}: ${row[index]}`).join('\n')).moveDown()
  }
  document.end()
  return new Response(new Uint8Array(await finished), { headers: { 'content-type': 'application/pdf', 'content-disposition': disposition } })
}
