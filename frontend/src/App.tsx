import { lazy, Suspense, useCallback, useEffect, useState, type CSSProperties } from 'react';
import {
  BrowserRouter,
  Navigate,
  Route,
  Routes,
  useLocation,
  useParams,
  useSearchParams,
} from 'react-router-dom';

import { api, isAuthError, onUnauthorized } from './api/client';
import type { KbView, OkResponse, UserInfo } from './api/types';
import {
  clearAuthSession,
  getAuthSession,
  hasDashboardAccess,
  isSystemAdmin,
  setAuthSession,
  subscribeAuthCrossTab,
  type AuthSession,
} from './auth';
import AppSidebar from './components/AppSidebar';
import { NavigationSectionContext } from './components/NavigationSectionContext';
import { activePrimaryNavigationItem } from '@/lib/navigation';
import { SidebarProvider } from '@/components/ui/sidebar';
import { Toaster } from '@/components/ui/sonner';
import { TooltipProvider } from '@/components/ui/tooltip';
import { notify } from '@/components/ui/app-toast';
import LoginPage from './pages/LoginPage';
const KbListPage = lazy(() => import('./pages/KbListPage'));
const HelpPage = lazy(() => import('./pages/HelpPage'));
const ChatPage = lazy(() => import('./pages/chat/ChatPage'));
const TasksPage = lazy(() => import('./pages/TasksPage'));
const KbFilesPage = lazy(() => import('./pages/KbFilesPage'));
const DocumentAssetsPage = lazy(() => import('./pages/DocumentAssetsPage'));
const WikiPage = lazy(() => import('./pages/WikiPage'));
const UsersPage = lazy(() => import('./pages/admin/UsersPage'));
const DepartmentsPage = lazy(() => import('./pages/admin/DepartmentsPage'));
const KbMountPage = lazy(() => import('./pages/admin/KbMountPage'));
const GovernancePage = lazy(() => import('./pages/admin/GovernancePage'));
const LogsPage = lazy(() => import('./pages/admin/LogsPage'));
const FeedbackTicketsPage = lazy(() => import('./pages/admin/FeedbackTicketsPage'));
const CostsPage = lazy(() => import('./pages/admin/CostsPage'));
const ConfigPage = lazy(() => import('./pages/admin/ConfigPage'));
const EvaluationPage = lazy(() => import('./pages/admin/EvaluationPage'));
const SystemStatusPage = lazy(() => import('./pages/admin/SystemStatusPage'));
const PluginsPage = lazy(() => import('./pages/admin/PluginsPage'));
const ExtensionsPage = lazy(() => import('./pages/ExtensionsPage'));
const MemoryPage = lazy(() => import('./pages/MemoryPage'));

const SIDEBAR_STORAGE_KEY = 'hdb_sidebar_expanded';

function KbChatRedirect() {
  const { kbName = '' } = useParams();
  return <Navigate to={`/chat?kb=${encodeURIComponent(decodeURIComponent(kbName))}`} replace />;
}

function ChatRoute({
  auth,
  onLogout,
  kbs,
}: {
  auth: AuthSession;
  onLogout: () => void;
  kbs: KbView[];
}) {
  const [searchParams] = useSearchParams();
  const kbName = searchParams.get('kb') || '';
  return <ChatPage auth={auth} onLogout={onLogout} kbName={kbName} availableKbs={kbs} />;
}

function KbFilesRoute({ auth, onLogout }: { auth: AuthSession; onLogout: () => void }) {
  const { kbName = '' } = useParams();
  return (
    <KbFilesPage key={kbName} auth={auth} onLogout={onLogout} kbName={decodeURIComponent(kbName)} />
  );
}

function HomeRedirect({ auth }: { auth: AuthSession }) {
  return <Navigate to={isSystemAdmin(auth.user) ? '/admin/governance' : '/assets'} replace />;
}

function KbContentRoute({
  auth,
  children,
}: {
  auth: AuthSession;
  children: React.ReactNode;
}) {
  if (isSystemAdmin(auth.user)) {
    return <Navigate to="/admin/governance" replace />;
  }
  return <>{children}</>;
}

/** 管理路由守卫:治理面板/日志对员工开放(部门级);系统状态/账号/配置等仅 sysadmin。 */
function AdminRoute({
  auth,
  requireSysAdmin,
  children,
}: {
  auth: AuthSession;
  requireSysAdmin?: boolean;
  children: React.ReactNode;
}) {
  if (!hasDashboardAccess(auth.user)) {
    return <Navigate to="/kbs" replace />;
  }
  if (requireSysAdmin && !isSystemAdmin(auth.user)) {
    return <Navigate to="/kbs" replace />;
  }
  return <>{children}</>;
}

function Shell({ auth, onLogout }: { auth: AuthSession; onLogout: () => void }) {
  const location = useLocation();
  const primaryNavigation = activePrimaryNavigationItem(auth.user, location.pathname);
  const [kbs, setKbs] = useState<KbView[]>([]);
  const [kbsLoaded, setKbsLoaded] = useState(false);
  const sysAdmin = isSystemAdmin(auth.user);
  const [sidebarExpanded, setSidebarExpanded] = useState(() => {
    const stored = window.localStorage.getItem(SIDEBAR_STORAGE_KEY);
    return stored == null ? true : stored === '1';
  });

  const loadKbs = useCallback(() => {
    if (sysAdmin) {
      setKbs([]);
      setKbsLoaded(true);
      return undefined;
    }
    let cancelled = false;
    setKbsLoaded(false);
    api
      .get<KbView[]>('/api/v1/kbs')
      .then((rows) => {
        if (!cancelled) setKbs(rows);
      })
      .catch((error) => {
        if (!cancelled) notify.error(error instanceof Error ? error.message : '加载知识库失败');
      })
      .finally(() => {
        if (!cancelled) setKbsLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [sysAdmin]);

  useEffect(() => {
    const cancel = loadKbs();
    return cancel;
  }, [loadKbs]);

  function handleSidebarOpenChange(open: boolean) {
    setSidebarExpanded(open);
    window.localStorage.setItem(SIDEBAR_STORAGE_KEY, open ? '1' : '0');
  }

  return (
    <SidebarProvider
      open={sidebarExpanded}
      onOpenChange={handleSidebarOpenChange}
      style={{ '--sidebar-width': '240px', '--sidebar-width-icon': '72px' } as CSSProperties}
      className="app-shell"
    >
      <AppSidebar auth={auth} onLogout={onLogout} />
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <div className="content flex-1">
          <NavigationSectionContext.Provider
            value={{
              primary: primaryNavigation,
              subnav: primaryNavigation?.items,
              sectionLabel: primaryNavigation?.id === 'governance'
                ? '治理与运营'
                : primaryNavigation?.id === 'management'
                  ? '组织与平台管理'
                  : primaryNavigation?.id === 'extensions'
                    ? '个人能力'
                    : undefined,
            }}
          >
          <Suspense fallback={<div role="status" className="p-6 text-muted-foreground">正在加载页面…</div>}>
          <Routes>
            <Route path="/" element={<HomeRedirect auth={auth} />} />
            <Route path="/admin/quality" element={<AdminRoute auth={auth}><FeedbackTicketsPage auth={auth} onLogout={onLogout} /></AdminRoute>} />
            <Route path="/admin/quality/advanced" element={<Navigate to="/admin/quality" replace />} />
            <Route path="/admin/costs" element={<AdminRoute auth={auth}><CostsPage auth={auth} onLogout={onLogout} /></AdminRoute>} />
            <Route
              path="/assets"
              element={
                <KbContentRoute auth={auth}>
                  <DocumentAssetsPage auth={auth} onLogout={onLogout} kbs={kbs} />
                </KbContentRoute>
              }
            />
            <Route
              path="/wiki"
              element={
                <KbContentRoute auth={auth}>
                  <WikiPage auth={auth} onLogout={onLogout} kbs={kbs} />
                </KbContentRoute>
              }
            />
            <Route
              path="/kbs"
              element={
                <KbContentRoute auth={auth}>
                  <KbListPage
                    auth={auth}
                    kbs={kbs}
                    kbsLoaded={kbsLoaded}
                    onLogout={onLogout}
                    onRefresh={() => void loadKbs()}
                  />
                </KbContentRoute>
              }
            />
            <Route
              path="/chat"
              element={
                <KbContentRoute auth={auth}>
                  <ChatRoute auth={auth} onLogout={onLogout} kbs={kbs} />
                </KbContentRoute>
              }
            />
            <Route
              path="/tasks"
              element={
                <KbContentRoute auth={auth}>
                  <TasksPage kbs={kbs} />
                </KbContentRoute>
              }
            />
            <Route
              path="/memory"
              element={
                <KbContentRoute auth={auth}>
                  <MemoryPage auth={auth} onLogout={onLogout} kbs={kbs} />
                </KbContentRoute>
              }
            />
            <Route
              path="/extensions"
              element={
                <KbContentRoute auth={auth}>
                  <ExtensionsPage auth={auth} />
                </KbContentRoute>
              }
            />
            <Route
              path="/kbs/:kbName/chat/*"
              element={
                <KbContentRoute auth={auth}>
                  <KbChatRedirect />
                </KbContentRoute>
              }
            />
            <Route
              path="/kbs/:kbName/files"
              element={
                <KbContentRoute auth={auth}>
                  <KbFilesRoute auth={auth} onLogout={onLogout} />
                </KbContentRoute>
              }
            />
            <Route
              path="/admin/users"
              element={
                <AdminRoute auth={auth} requireSysAdmin>
                  <UsersPage auth={auth} onLogout={onLogout} />
                </AdminRoute>
              }
            />
            <Route
              path="/admin/departments"
              element={
                <AdminRoute auth={auth} requireSysAdmin>
                  <DepartmentsPage auth={auth} onLogout={onLogout} />
                </AdminRoute>
              }
            />
            <Route
              path="/admin/kb-mount"
              element={
                <AdminRoute auth={auth} requireSysAdmin>
                  <KbMountPage auth={auth} onLogout={onLogout} />
                </AdminRoute>
              }
            />
            <Route
              path="/admin/governance"
              element={
                <AdminRoute auth={auth}>
                  <GovernancePage auth={auth} onLogout={onLogout} />
                </AdminRoute>
              }
            />
            <Route
              path="/admin/logs"
              element={
                <AdminRoute auth={auth}>
                  <LogsPage auth={auth} onLogout={onLogout} />
                </AdminRoute>
              }
            />
            <Route
              path="/admin/config"
              element={
                <AdminRoute auth={auth} requireSysAdmin>
                  <ConfigPage auth={auth} onLogout={onLogout} />
                </AdminRoute>
              }
            />
            <Route
              path="/admin/evaluation"
              element={
                <AdminRoute auth={auth} requireSysAdmin>
                  <EvaluationPage auth={auth} onLogout={onLogout} />
                </AdminRoute>
              }
            />
            <Route
              path="/admin/status"
              element={
                <AdminRoute auth={auth} requireSysAdmin>
                  <SystemStatusPage auth={auth} onLogout={onLogout} />
                </AdminRoute>
              }
            />
            <Route
              path="/admin/plugins"
              element={
                <AdminRoute auth={auth} requireSysAdmin>
                  <PluginsPage auth={auth} />
                </AdminRoute>
              }
            />
            <Route path="*" element={<HomeRedirect auth={auth} />} />
          </Routes>
          </Suspense>
          </NavigationSectionContext.Provider>
        </div>
      </div>
    </SidebarProvider>
  );
}

/** 业务页使用应用壳；使用帮助在登录校验后提供独立手册布局。 */
function AuthedApp({ auth, onLogout }: { auth: AuthSession; onLogout: () => void }) {
  const location = useLocation();
  if (/^\/help\/?$/.test(location.pathname)) {
    return <Suspense fallback={<div role="status" className="p-8 text-muted-foreground">正在加载使用手册…</div>}><HelpPage /></Suspense>;
  }
  if ((location.pathname === '/chat' || /^\/kbs\/[^/]+\/chat(?:\/.*)?$/.test(location.pathname)) && isSystemAdmin(auth.user)) {
    return <Navigate to="/admin/governance" replace />;
  }
  return <Shell auth={auth} onLogout={onLogout} />;
}

export default function App() {
  const [auth, setAuth] = useState<AuthSession | null>(() => getAuthSession());
  const [authChecked, setAuthChecked] = useState(() => !auth?.token);

  // 启动时用 whoami 校验本地 token 仍有效(被撤销/停用则强制重登)
  useEffect(() => {
    if (!auth?.token) {
      setAuthChecked(true);
      return undefined;
    }
    let cancelled = false;
    setAuthChecked(false);
    api
      .get<UserInfo>('/api/v1/whoami')
      .then((user) => {
        if (cancelled) return;
        const refreshed = { token: auth.token, user };
        setAuthSession(refreshed);
        setAuth(refreshed);
        setAuthChecked(true);
      })
      .catch((error) => {
        if (cancelled) return;
        if (isAuthError(error)) {
          clearAuthSession();
          setAuth(null);
        }
        // 网络类错误不清 session(后端短暂不可达不应踢人)
        setAuthChecked(true);
      });
    return () => {
      cancelled = true;
    };
    // 仅在 token 变化时重校验
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [auth?.token]);

  const handleLogout = useCallback(() => {
    // 先撤销服务端 session,再清本地;失败也照样清本地
    void api.post<OkResponse>('/api/v1/logout').catch(() => undefined);
    clearAuthSession();
    setAuth(null);
    setAuthChecked(true);
  }, []);

  // 全局 401(token 失效/被撤销)与其他 tab 登出:清本地会话并回到登录页。
  // auth 置 null 后由 catch-all 路由直接渲染 LoginPage(与登出一致的渲染路径,无需命令式 navigate)。
  useEffect(() => {
    const forceRelogin = () => {
      // 有意不复用 handleLogout:这里不能再调 /api/v1/logout,
      // 否则该请求自身的 401 会再次触发处理器造成死循环
      clearAuthSession();
      setAuth(null);
      setAuthChecked(true);
    };
    onUnauthorized(forceRelogin);
    const unsubscribeCrossTab = subscribeAuthCrossTab(forceRelogin);
    return () => {
      onUnauthorized(null);
      unsubscribeCrossTab();
    };
  }, []);

  return (
    <TooltipProvider>
      <BrowserRouter>
        <Routes>
          <Route
            path="/*"
            element={
              auth == null ? (
                authChecked ? (
                  <LoginPage
                    onLogin={(session) => {
                      setAuthSession(session);
                      setAuth(session);
                    }}
                  />
                ) : null
              ) : !authChecked ? null : (
                <AuthedApp auth={auth} onLogout={handleLogout} />
              )
            }
          />
        </Routes>
      </BrowserRouter>
      <Toaster richColors closeButton position="top-center" />
    </TooltipProvider>
  );
}
