import { spawn } from 'node:child_process'
import { createServer } from 'node:net'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const frontendDir = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const repoDir = resolve(frontendDir, '..')
const projectName = `museflow-compose-e2e-${process.pid}`
const runId = `compose-e2e-${process.pid}-${Date.now().toString(36)}`
const allocatedPorts = new Set()
const playwrightArguments = process.argv.slice(2).filter((argument) => (
  argument !== '--skip-restart'
  && argument !== '--backend-check'
  && argument !== '--full-backend-gates'
))
const skipRestartCheck = process.argv.includes('--skip-restart')
const runBackendCheck = process.argv.includes('--backend-check')
const runFullBackendGates = process.argv.includes('--full-backend-gates')
if (runFullBackendGates && !runBackendCheck) {
  throw new Error('--full-backend-gates requires --backend-check.')
}

async function freePort() {
  const server = createServer()
  await new Promise((resolveListen, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', resolveListen)
  })
  const address = server.address()
  if (!address || typeof address === 'string') throw new Error('Failed to reserve a local port')
  const port = address.port
  await new Promise((resolveClose, reject) => server.close((error) => error ? reject(error) : resolveClose()))
  if (allocatedPorts.has(port)) return freePort()
  allocatedPorts.add(port)
  return port
}

function run(command, args, options) {
  return new Promise((resolveRun, reject) => {
    const child = spawn(command, args, { stdio: 'inherit', ...options })
    child.once('error', reject)
    child.once('exit', (code, signal) => {
      if (signal) reject(new Error(`${command} exited on ${signal}`))
      else resolveRun(code ?? 1)
    })
  })
}

const ports = {
  MUSEFLOW_WEB_PORT: await freePort(),
  MUSEFLOW_API_PORT: await freePort(),
  MUSEFLOW_POSTGRES_PORT: await freePort(),
  MUSEFLOW_REDIS_PORT: await freePort(),
  MUSEFLOW_MINIO_PORT: await freePort(),
  MUSEFLOW_MINIO_CONSOLE_PORT: await freePort(),
}
const env = {
  ...process.env,
  ...ports,
  MUSEFLOW_PROVIDER: 'mock',
  DASHSCOPE_API_KEY: '',
  DASHSCOPE_API_HOST: '',
  ...(runFullBackendGates ? {
    MUSEFLOW_LEASE_SECONDS: '3',
    MUSEFLOW_HEARTBEAT_INTERVAL_SECONDS: '0.5',
  } : {}),
  MINIO_PUBLIC_ENDPOINT: `http://127.0.0.1:${ports.MUSEFLOW_MINIO_PORT}`,
  PLAYWRIGHT_BASE_URL: `http://127.0.0.1:${ports.MUSEFLOW_WEB_PORT}`,
  MUSEFLOW_E2E_RUN_ID: runId,
}
delete env.NO_COLOR
delete env.FORCE_COLOR
const docker = process.platform === 'win32' ? 'docker.exe' : 'docker'
const composePrefix = ['compose', '--project-name', projectName]
let failureCode = 0

try {
  console.log(`Starting isolated Compose project ${projectName} with MockProvider.`)
  failureCode = await run(docker, [...composePrefix, 'up', '--build', '--detach', '--wait', '--wait-timeout', '240'], {
    cwd: repoDir,
    env,
  })
  if (failureCode === 0) {
    console.log(`Running Playwright against ${env.PLAYWRIGHT_BASE_URL}.`)
    const playwrightCli = resolve(frontendDir, 'node_modules', 'playwright', 'cli.js')
    failureCode = await run(process.execPath, [playwrightCli, 'test', '--config', 'playwright.config.ts', ...playwrightArguments], {
      cwd: frontendDir,
      env,
    })
  }
  if (failureCode === 0 && runBackendCheck) {
    const testDatabase = `${projectName.replaceAll('-', '_')}_tests`
    const testDatabaseURL = `postgresql+psycopg://museflow:museflow@127.0.0.1:${ports.MUSEFLOW_POSTGRES_PORT}/${testDatabase}`
    const backendEnv = {
      ...env,
      MUSEFLOW_DATABASE_URL: testDatabaseURL,
      MUSEFLOW_TEST_DATABASE_URL: testDatabaseURL,
      MUSEFLOW_PROVIDER: 'mock',
      DASHSCOPE_API_KEY: '',
      DASHSCOPE_API_HOST: '',
      REDIS_URL: `redis://127.0.0.1:${ports.MUSEFLOW_REDIS_PORT}/0`,
      MINIO_ENDPOINT: `http://127.0.0.1:${ports.MUSEFLOW_MINIO_PORT}`,
      MINIO_PUBLIC_ENDPOINT: `http://127.0.0.1:${ports.MUSEFLOW_MINIO_PORT}`,
      MINIO_ACCESS_KEY: 'minioadmin',
      MINIO_SECRET_KEY: 'minioadmin',
      MINIO_BUCKET: 'museflow-results',
      MUSEFLOW_LEASE_SECONDS: '240',
      MUSEFLOW_HEARTBEAT_INTERVAL_SECONDS: '30',
    }
    for (const key of Object.keys(backendEnv)) {
      if (key.startsWith('MUSEFLOW_RUN_')) backendEnv[key] = '0'
    }
    const uv = process.platform === 'win32' ? 'uv.exe' : 'uv'
    console.log(`Running full backend regressions against isolated PostgreSQL database ${testDatabase}.`)
    failureCode = await run(docker, [...composePrefix, 'exec', '--no-TTY', 'postgres', 'createdb', '-U', 'museflow', testDatabase], {
      cwd: repoDir,
      env: backendEnv,
    })
    if (failureCode === 0) {
      failureCode = await run(uv, ['run', '--directory', 'backend', 'alembic', 'upgrade', 'head'], {
        cwd: repoDir,
        env: backendEnv,
      })
    }
    if (failureCode === 0) {
      failureCode = await run(uv, ['run', '--directory', 'backend', 'alembic', 'check'], {
        cwd: repoDir,
        env: backendEnv,
      })
    }
    if (failureCode === 0) {
      failureCode = await run(uv, ['run', '--directory', 'backend', 'alembic', 'current'], {
        cwd: repoDir,
        env: backendEnv,
      })
    }
    if (failureCode === 0) {
      failureCode = await run(uv, ['run', '--directory', 'backend', 'pytest', '-q', '-rs'], {
        cwd: repoDir,
        env: backendEnv,
      })
    }
    if (failureCode === 0 && runFullBackendGates) {
      const serviceDatabaseURL = `postgresql+psycopg://museflow:museflow@127.0.0.1:${ports.MUSEFLOW_POSTGRES_PORT}/museflow`
      const gateEnv = {
        ...env,
        MUSEFLOW_DATABASE_URL: serviceDatabaseURL,
        MUSEFLOW_TEST_DATABASE_URL: serviceDatabaseURL,
        MUSEFLOW_PROVIDER: 'mock',
        DASHSCOPE_API_KEY: '',
        DASHSCOPE_API_HOST: '',
        REDIS_URL: `redis://127.0.0.1:${ports.MUSEFLOW_REDIS_PORT}/0`,
        MINIO_ENDPOINT: `http://127.0.0.1:${ports.MUSEFLOW_MINIO_PORT}`,
        MINIO_PUBLIC_ENDPOINT: `http://127.0.0.1:${ports.MUSEFLOW_MINIO_PORT}`,
        MINIO_ACCESS_KEY: 'minioadmin',
        MINIO_SECRET_KEY: 'minioadmin',
        MINIO_BUCKET: 'museflow-results',
        MUSEFLOW_LEASE_SECONDS: '3',
        MUSEFLOW_HEARTBEAT_INTERVAL_SECONDS: '0.5',
      }
      const gateFlags = [
        'MUSEFLOW_RUN_REAL_IMAGE_TO_IMAGE_TEST',
        'MUSEFLOW_RUN_REAL_COMPOSE_E2E_TEST',
        'MUSEFLOW_RUN_SIMULATED_DASHSCOPE_E2E',
        'MUSEFLOW_RUN_REAL_MINIO_TEST',
        'MUSEFLOW_RUN_REAL_REDIS_TEST',
        'MUSEFLOW_RUN_REAL_MAINTENANCE_TEST',
        'MUSEFLOW_RUN_REFERENCE_MINIO_TEST',
        'MUSEFLOW_RUN_RESULT_PUBLICATION_INTEGRATION',
      ]
      for (const flag of gateFlags) gateEnv[flag] = '0'
      for (const flag of [
        'MUSEFLOW_RUN_REAL_IMAGE_TO_IMAGE_TEST',
        'MUSEFLOW_RUN_REAL_COMPOSE_E2E_TEST',
        'MUSEFLOW_RUN_REAL_MINIO_TEST',
        'MUSEFLOW_RUN_REAL_REDIS_TEST',
        'MUSEFLOW_RUN_REAL_MAINTENANCE_TEST',
        'MUSEFLOW_RUN_REFERENCE_MINIO_TEST',
      ]) gateEnv[flag] = '1'

      const uv = process.platform === 'win32' ? 'uv.exe' : 'uv'
      console.log('Running Compose-worker, Redis, MinIO, and maintenance integration tests.')
      failureCode = await run(uv, [
        'run', '--directory', 'backend', 'pytest', '-q', '-rs',
        'tests/integration/test_real_compose_image_to_image_e2e.py',
        'tests/integration/test_real_compose_mock_e2e.py',
        'tests/integration/test_real_minio_smoke.py',
        'tests/integration/test_real_redis_worker.py',
        'tests/integration/test_real_reference_maintenance_worker.py',
        'tests/integration/test_reference_asset_minio.py',
      ], { cwd: repoDir, env: gateEnv })

      if (failureCode === 0) {
        console.log('Stopping only the generation worker so test-owned workers have exclusive queue access.')
        failureCode = await run(docker, [...composePrefix, 'stop', 'worker'], { cwd: repoDir, env })
      }
      if (failureCode === 0) {
        gateEnv.MUSEFLOW_RUN_SIMULATED_DASHSCOPE_E2E = '1'
        console.log('Running the simulated DashScope integration test in its own worker process.')
        failureCode = await run(uv, [
          'run', '--directory', 'backend', 'pytest', '-q', '-rs',
          'tests/integration/test_real_compose_simulated_dashscope_e2e.py',
        ], { cwd: repoDir, env: gateEnv })
      }
      if (failureCode === 0) {
        gateEnv.MUSEFLOW_RUN_SIMULATED_DASHSCOPE_E2E = '0'
        gateEnv.MUSEFLOW_RUN_RESULT_PUBLICATION_INTEGRATION = '1'
        console.log('Running result-publication integration tests in their own worker process.')
        failureCode = await run(uv, [
          'run', '--directory', 'backend', 'pytest', '-q', '-rs',
          'tests/integration/test_result_publication.py',
        ], { cwd: repoDir, env: gateEnv })
      }
    }
  }
  if (failureCode === 0 && !skipRestartCheck) {
    console.log('Stopping and restarting the isolated project to verify database and object storage persistence.')
    failureCode = await run(docker, [...composePrefix, 'stop'], { cwd: repoDir, env })
    if (failureCode === 0) {
      failureCode = await run(docker, [...composePrefix, 'up', '--detach', '--wait', '--wait-timeout', '180'], {
        cwd: repoDir,
        env,
      })
    }
    if (failureCode === 0) {
      try {
        const root = `${env.PLAYWRIGHT_BASE_URL}/api/v1`
        let cursor
        let savedTask
        for (let pageNumber = 0; pageNumber < 10 && !savedTask; pageNumber += 1) {
          const query = new URLSearchParams({ limit: '20' })
          if (cursor) query.set('cursor', cursor)
          const response = await fetch(`${root}/tasks?${query}`)
          if (!response.ok) throw new Error(`History read failed after restart (${response.status}).`)
          const listing = await response.json()
          savedTask = listing.items.find((task) => task.prompt === `${runId}:persist:text:雨后山谷木屋`)
          cursor = listing.next_cursor
          if (!cursor) break
        }
        if (!savedTask) throw new Error('The marked successful task was not present in PostgreSQL after restart.')
        const response = await fetch(`${root}/tasks/${savedTask.id}`)
        if (!response.ok) throw new Error(`Task read failed after restart (${response.status}).`)
        const task = await response.json()
        if (task.status !== 'SUCCEEDED' || !task.result?.download_url) {
          throw new Error('The persisted task or result metadata changed after restart.')
        }
        const result = await fetch(new URL(task.result.download_url, env.PLAYWRIGHT_BASE_URL))
        if (!result.ok || !result.headers.get('content-type')?.includes('image/png')) {
          throw new Error(`The persisted MinIO result was unavailable after restart (${result.status}).`)
        }
        console.log('Restart persistence passed: task metadata and stable result download remain available.')
      } catch (error) {
        console.error(error instanceof Error ? error.message : 'Restart persistence verification failed.')
        failureCode = 1
      }
    }
  }
  if (failureCode !== 0) {
    console.log('Showing only the isolated generation-worker and scheduler logs before cleanup.')
    await run(docker, [...composePrefix, 'logs', '--tail', '80', 'worker', 'scheduler'], { cwd: repoDir, env })
  }
} catch (error) {
  console.error(error instanceof Error ? error.message : 'Compose UI E2E failed.')
  failureCode = 1
} finally {
  console.log(`Removing only isolated Compose project ${projectName} and its volumes.`)
  const cleanupCode = await run(docker, [...composePrefix, 'down', '--volumes', '--remove-orphans'], {
    cwd: repoDir,
    env,
  }).catch((error) => {
    console.error(error instanceof Error ? error.message : 'Compose cleanup failed.')
    return 1
  })
  if (cleanupCode !== 0 && failureCode === 0) failureCode = cleanupCode
}

process.exitCode = failureCode
