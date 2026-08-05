import { useCallback, useEffect, useState, type CSSProperties } from 'react';
import {
  BrowserRouter,
  Navigate,
  Route,
  Routes,
  useLocation,
  useParams,
  useSearchParams,
} from 'react-router-dom';

import { api, isAuthError } from './api/client';
import type { KbView, OkResponse, UserInfo } from './api/types';
import {
  clearAuthSession,
  getAuthSession,
  isAnyAdmin,
  isSystemAdmin,
  setAuthSession,
  type AuthSession,
} from './auth';
import AppSidebar from './components/AppSidebar';
import { SidebarProvider } from '@/components/ui/sidebar';
import { Toaster } from '@/components/ui/sonner';
import { TooltipProvider } from '@/components/ui/tooltip';
import { notify } from '@/components/ui/app-toast';
import LoginPage from './pages/LoginPage';
import KbListPage from './pages/KbListPage';
import ChatPage from './pages/chat/ChatPage';
import KbFilesPage from './pages/KbFilesPage';
import AssetsPage from './pages/AssetsPage';
import DocumentGenerationPage from './pages/DocumentGenerationPage';
import UsersPage from './pages/admin/UsersPage';
import DepartmentsPage from './pages/admin/DepartmentsPage';
import KbPermissionsPage from './pages/admin/KbPermissionsPage';
import GovernancePage from './pages/admin/GovernancePage';
import LogsPage from './pages/admin/LogsPage';
import ConfigPage from './pages/admin/ConfigPage';
import EvaluationPage from './pages/admin/EvaluationPage';

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

/** 管理路由守卫:非任意 admin -> 回 /kbs;部门管理页仅 sysadmin。 */
function AdminRoute({
  auth,
  requireSysAdmin,
  children,
}: {
  auth: AuthSession;
  requireSysAdmin?: boolean;
  children: React.ReactNode;
}) {
  if (!isAnyAdmin(auth.user)) {
    return <Navigate to="/kbs" replace />;
  }
  if (requireSysAdmin && !isSystemAdmin(auth.user)) {
    return <Navigate to="/kbs" replace />;
  }
  return <>{children}</>;
}

function Shell({ auth, onLogout }: { auth: AuthSession; onLogout: () => void }) {
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
          <Routes>
            <Route path="/" element={<HomeRedirect auth={auth} />} />
            <Route
              path="/assets"
              element={
                <KbContentRoute auth={auth}>
                  <AssetsPage auth={auth} onLogout={onLogout} kbs={kbs} />
                </KbContentRoute>
              }
            />
            <Route
              path="/document-generation"
              element={
                <KbContentRoute auth={auth}>
                  <DocumentGenerationPage auth={auth} onLogout={onLogout} />
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
                <AdminRoute auth={auth}>
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
              path="/admin/kb-permissions"
              element={
                <AdminRoute auth={auth}>
                  <KbPermissionsPage auth={auth} onLogout={onLogout} />
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
            <Route path="*" element={<HomeRedirect auth={auth} />} />
          </Routes>
        </div>
      </div>
    </SidebarProvider>
  );
}

/** 已登录应用统一走 Shell;聊天作为一级业务页挂在全局侧边栏内。 */
function AuthedApp({ auth, onLogout }: { auth: AuthSession; onLogout: () => void }) {
  const location = useLocation();
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
