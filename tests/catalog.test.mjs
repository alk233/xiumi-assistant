// Offline regressions for the template-catalog cache helpers. These functions were
// deleted together with xiumi_study and left xiumi_catalog as a ReferenceError on
// every call; guard both the presence and the cache semantics.
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, writeFileSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'

import { catalogIsFresh, readCatalogFile, writeCatalogFile } from '../index.js'

const DAY = 24 * 60 * 60 * 1000

function tempFile(name = 'template-catalog.json') {
  const dir = mkdtempSync(join(tmpdir(), 'xiumi-catalog-'))
  return { dir, file: join(dir, name) }
}

test('a readable catalog file is returned as parsed JSON', () => {
  const { dir, file } = tempFile()
  try {
    writeFileSync(file, JSON.stringify({ schemaVersion: 1, groups: [{ name: '标题' }] }), 'utf8')
    assert.deepEqual(readCatalogFile(file), { schemaVersion: 1, groups: [{ name: '标题' }] })
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('a corrupt or missing catalog file reads as null instead of throwing', () => {
  const { dir, file } = tempFile()
  try {
    writeFileSync(file, '{ not json', 'utf8')
    assert.equal(readCatalogFile(file), null)
    assert.equal(readCatalogFile(join(dir, 'absent.json')), null)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('a fresh catalog of the current schema version is reused', () => {
  const now = new Date().toISOString()
  assert.equal(catalogIsFresh({ schemaVersion: 1, lastVerifiedAt: now }), true)
  assert.equal(catalogIsFresh({ schemaVersion: 1, capturedAt: now }), true)
})

test('an outdated schema version invalidates the cache', () => {
  assert.equal(catalogIsFresh({ schemaVersion: 2, lastVerifiedAt: new Date().toISOString() }), false)
  assert.equal(catalogIsFresh({ lastVerifiedAt: new Date().toISOString() }), false)
})

test('a stale or timestamp-less catalog invalidates the cache', () => {
  assert.equal(catalogIsFresh({ schemaVersion: 1, lastVerifiedAt: new Date(Date.now() - 31 * DAY).toISOString() }), false)
  assert.equal(catalogIsFresh({ schemaVersion: 1 }), false)
  assert.equal(catalogIsFresh({ schemaVersion: 1, lastVerifiedAt: 'not-a-date' }), false)
})

test('writing a catalog replaces the file atomically and can be read back', () => {
  const { dir, file } = tempFile()
  try {
    const catalog = { schemaVersion: 1, lastVerifiedAt: new Date().toISOString(), groups: [{ name: '卡片' }] }
    assert.equal(writeCatalogFile(catalog, file), true)
    assert.deepEqual(JSON.parse(readFileSync(file, 'utf8')), catalog)
    assert.equal(writeCatalogFile(catalog, file), true)
    assert.deepEqual(JSON.parse(readFileSync(file, 'utf8')), catalog)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('writing an invalid catalog refuses without touching the file', () => {
  const { dir, file } = tempFile()
  try {
    assert.equal(writeCatalogFile(null, file), false)
    assert.equal(writeCatalogFile([{ name: 'x' }], file), false)
    assert.equal(writeCatalogFile('string', file), false)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})
