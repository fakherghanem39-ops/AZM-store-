import { useState, useEffect } from "react";
import { getUser } from "./lib/api";

import Home from "./pages/user/Home";
import MyOrders from "./pages/user/MyOrders";
import MyDeposits from "./pages/user/MyDeposits";
import Deposit from "./pages/user/Deposit";
import Support from "./pages/user/Support";
import AdminRoot from "./pages/admin/AdminRoot";

const ADMIN_IDS = [6200238604, 7286288857];

declare global {
  interface Window {
    Telegram?: {
      WebApp?: {
        initDataUnsafe?: { user?: { id?: number; first_name?: string; last_name?: string; username?: string } };
        expand?: () => void;
        ready?: () => void;
        colorScheme?: string;
      };
    };
  }
}

function getTelegramUser() {
  try {
    const tg = window.Telegram?.WebApp;
    if (tg) {
      tg.expand?.();
      tg.ready?.();
      return tg.initDataUnsafe?.user || null;
    }
  } catch {}
  return null;
}

type TabId = "home" | "deposit" | "orders" | "deposits" | "support" | "admin";

const NAV_ITEMS = [
  { id: "support" as TabId,  label: "اتصل بنا",   icon: "🎧" },
  { id: "deposits" as TabId, label: "سجل الشحن",  icon: "📋" },
  { id: "deposit" as TabId,  label: "شحن",        icon: "+",  center: true },
  { id: "orders" as TabId,   label: "طلباتي",     icon: "📦" },
  { id: "home" as TabId,     label: "الرئيسية",   icon: "🏠" },
];

export default function App() {
  const [tab, setTab] = useState<TabId>("home");
  const [balance, setBalance] = useState<number>(0);
  const tgUser = getTelegramUser();
  const userId: number = tgUser?.id ?? 0;
  const isAdmin = ADMIN_IDS.includes(userId);

  useEffect(() => {
    if (userId) {
      getUser(userId).then(u => setBalance(u?.balance ?? 0)).catch(() => {});
    }
  }, [userId]);

  const refreshBalance = () => {
    if (userId) getUser(userId).then(u => setBalance(u?.balance ?? 0)).catch(() => {});
  };

  const renderPage = () => {
    switch (tab) {
      case "home":     return <Home userId={userId} balance={balance} tgUser={tgUser} onDepositClick={() => setTab("deposit")} />;
      case "deposit":  return <Deposit userId={userId} tgUser={tgUser} onSuccess={refreshBalance} />;
      case "orders":   return <MyOrders userId={userId} />;
      case "deposits": return <MyDeposits userId={userId} />;
      case "support":  return <Support />;
      case "admin":    return isAdmin ? <AdminRoot /> : null;
      default:         return <Home userId={userId} balance={balance} tgUser={tgUser} onDepositClick={() => setTab("deposit")} />;
    }
  };

  const displayItems = isAdmin
    ? [...NAV_ITEMS, { id: "admin" as TabId, label: "الإدارة", icon: "🛡️" }]
    : NAV_ITEMS;

  return (
    <div className="app-shell">
      {!tgUser && (
        <div className="tg-warning">
          ⚠️ افتح من داخل تيليغرام للحصول على تجربة كاملة
        </div>
      )}

      <div className="content">
        {renderPage()}
      </div>

      <nav className="bottom-nav">
        <div className="bottom-nav-inner">
          {displayItems.map(item => {
            if ((item as any).center) {
              return (
                <button
                  key={item.id}
                  className="nav-item-center"
                  onClick={() => setTab(item.id)}
                >
                  <div className="nav-center-circle" style={{
                    boxShadow: tab === item.id
                      ? "0 4px 20px rgba(224,82,82,0.7)"
                      : "0 4px 16px rgba(224,82,82,0.5)",
                  }}>
                    <span style={{ fontSize: 26, fontWeight: 900, color: "#fff", lineHeight: 1 }}>+</span>
                  </div>
                  <span style={{ color: tab === item.id ? "#fff" : "var(--text2)" }}>{item.label}</span>
                </button>
              );
            }
            return (
              <button
                key={item.id}
                className={`nav-item ${tab === item.id ? "active" : ""}`}
                onClick={() => setTab(item.id)}
              >
                <div className="nav-icon-wrap">
                  <span>{item.icon}</span>
                </div>
                <span>{item.label}</span>
              </button>
            );
          })}
        </div>
      </nav>
    </div>
  );
}
