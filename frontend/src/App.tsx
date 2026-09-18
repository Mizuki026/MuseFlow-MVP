import { Navigate, Route, Routes } from 'react-router-dom'
import { AppShell } from './components/AppShell'
import { NewTaskPage } from './pages/NewTaskPage'
import { TaskDetailPage } from './pages/TaskDetailPage'
import { TaskHistoryPage } from './pages/TaskHistoryPage'
import './App.css'

function App() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<Navigate to="/tasks/new" replace />} />
        <Route path="tasks/new" element={<NewTaskPage />} />
        <Route path="tasks/:taskId" element={<TaskDetailPage />} />
        <Route path="tasks" element={<TaskHistoryPage />} />
        <Route path="*" element={<Navigate to="/tasks/new" replace />} />
      </Route>
    </Routes>
  )
}

export default App
