import { Routes, Route } from "react-router-dom";
import Layout from "./components/Layout";
import Dashboard from "./pages/Dashboard";
import Sources from "./pages/Sources";
import Drafts from "./pages/Drafts";
import Archive from "./pages/Archive";
import Settings from "./pages/Settings";
import DraftEdit from "./pages/DraftEdit";
import Placeholder from "./pages/Placeholder";

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route path="/" element={<Dashboard />} />
        <Route path="/archive" element={<Archive />} />
        <Route path="/drafts" element={<Drafts />} />
        <Route path="/drafts/:id/edit" element={<DraftEdit />} />
        <Route path="/sources" element={<Sources />} />
        <Route path="/settings" element={<Settings />} />
        <Route path="*" element={<Placeholder title="未找到页面" />} />
      </Route>
    </Routes>
  );
}
