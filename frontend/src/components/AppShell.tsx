import { NavLink, Outlet } from 'react-router-dom'

export function AppShell() {
  return (
    <div className="app-shell">
      <header className="global-nav">
        <NavLink className="brand" to="/tasks/new" aria-label="MuseFlow 首页">
          <span className="brand-mark" aria-hidden="true">M</span>
          <span>MuseFlow</span>
        </NavLink>
        <nav aria-label="主导航">
          <NavLink to="/tasks/new">开始创作</NavLink>
          <NavLink to="/tasks" end>任务历史</NavLink>
        </nav>
      </header>
      <main className="page-shell"><Outlet /></main>
      <footer>Local creative system · trusted networks only</footer>
    </div>
  )
}
