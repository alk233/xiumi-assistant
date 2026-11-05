/**
 * 插件配置存储。
 *
 * 账号密码只保存在用户主目录下的 ~/.xiumi-assistant/config.json，不写进
 * 插件安装目录、不写日志、不通过 bridge 的 stdout 传递。读取失败时回退到
 * 空账号 + auto 主题，让设置页仍可正常使用。
 */
import {
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'
import { normalizeColorId, normalizeStyleId } from './theme.js'

export const DEFAULT_CONFIG = {
  account: '',
  password: '',
  stylePreset: 'auto',
  colorTheme: 'auto',
}

export function configDir() {
  return join(homedir(), '.xiumi-assistant')
}

export function configPath() {
  return join(configDir(), 'config.json')
}

export function catalogPath() {
  return join(configDir(), 'template-catalog.json')
}

function normalizeConfig(raw) {
  const value = raw && typeof raw === 'object' ? raw : {}
  return {
    account: typeof value.account === 'string' ? value.account : '',
    password: typeof value.password === 'string' ? value.password : '',
    stylePreset: normalizeStyleId(value.stylePreset),
    colorTheme: normalizeColorId(value.colorTheme),
  }
}

export function readConfig() {
  const filePath = configPath()
  try {
    if (!existsSync(filePath)) return { ...DEFAULT_CONFIG }
    return normalizeConfig(JSON.parse(readFileSync(filePath, 'utf8')))
  } catch {
    // 文件损坏或不可读时按默认值继续；不要把文件内容写进日志。
    return { ...DEFAULT_CONFIG }
  }
}

export function writeConfig(input) {
  const next = normalizeConfig(input)
  const directory = configDir()
  const filePath = configPath()
  const payload = JSON.stringify(next, null, 2)
  try {
    mkdirSync(directory, { recursive: true, mode: 0o700 })
    const temporary = `${filePath}.tmp-${process.pid}-${Date.now()}`
    writeFileSync(temporary, payload, { encoding: 'utf8', mode: 0o600 })
    try {
      renameSync(temporary, filePath)
    } catch {
      writeFileSync(filePath, payload, { encoding: 'utf8', mode: 0o600 })
      try {
        unlinkSync(temporary)
      } catch {
        // 忽略清理临时文件的失败。
      }
    }
  } catch (error) {
    throw new Error(`无法保存秀米助手配置：${error instanceof Error ? error.message : '未知错误'}`)
  }
  return next
}
