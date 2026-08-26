import type { CSSProperties } from 'react';

export type AppIconName =
  | 'arrow'
  | 'close'
  | 'database'
  | 'eye'
  | 'eye-off'
  | 'file'
  | 'folder'
  | 'grid'
  | 'history'
  | 'logout'
  | 'lock'
  | 'message'
  | 'plus'
  | 'refresh'
  | 'search'
  | 'send'
  | 'sidebar-close'
  | 'stop'
  | 'tool'
  | 'trash'
  | 'user'
  | 'warning';

type AppIconProps = {
  name: AppIconName;
  className?: string;
  size?: number;
  style?: CSSProperties;
};

const iconPaths: Record<AppIconName, string[]> = {
  arrow: ['M9 5l6 7-6 7'],
  close: ['M6 6l12 12', 'M18 6 6 18'],
  database: ['M5 7c0 2 14 2 14 0S5 5 5 7Z', 'M5 7v5c0 2 14 2 14 0V7', 'M5 12v5c0 2 14 2 14 0v-5'],
  eye: ['M3.5 12s3-5.5 8.5-5.5S20.5 12 20.5 12s-3 5.5-8.5 5.5S3.5 12 3.5 12Z', 'M12 14.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5Z'],
  'eye-off': ['M3.5 12s3-5.5 8.5-5.5c1.7 0 3.2.5 4.5 1.3', 'M20.5 12s-.9 1.7-2.5 3.2', 'M5 19L19 5'],
  file: ['M6 4h8l4 4v12H6V4Z', 'M14 4v5h5', 'M9 13h6', 'M9 16h4'],
  folder: ['M4 7h6l2 2h8v10H4V7Z'],
  grid: ['M5 5h5v5H5V5Z', 'M14 5h5v5h-5V5Z', 'M5 14h5v5H5v-5Z', 'M14 14h5v5h-5v-5Z'],
  history: ['M4 7v5h5', 'M4.8 12a7.2 7.2 0 1 0 2.1-5.1L4 9.8', 'M12 8v4l3 2'],
  logout: ['M9 5H6.5A2.5 2.5 0 0 0 4 7.5v9A2.5 2.5 0 0 0 6.5 19H9', 'M14 8l4 4-4 4', 'M18 12H9'],
  lock: ['M7 11h10v9H7v-9Z', 'M9 11V8a3 3 0 0 1 6 0v3'],
  message: ['M4 6h16v10H9l-5 4V6Z', 'M8 10h8', 'M8 13h5'],
  plus: ['M12 5v14', 'M5 12h14'],
  refresh: ['M19 8a7 7 0 0 0-12.2-2.4L5 8', 'M5 5v3h3', 'M5 16a7 7 0 0 0 12.2 2.4L19 16', 'M19 19v-3h-3'],
  search: ['M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14Z', 'M16.5 16.5 21 21'],
  send: ['M4 12 20 5l-7 14-2-6-7-1Z', 'M11 13l9-8'],
  'sidebar-close': ['M4 5h16v14H4V5Z', 'M9 5v14', 'M15 9l-3 3 3 3'],
  stop: ['M7 7h10v10H7V7Z'],
  tool: ['M14.5 5.5a4.5 4.5 0 0 0 4 6.3L11 19.3a2.1 2.1 0 0 1-3-3l7.5-7.5a4.5 4.5 0 0 0-1-3.3Z', 'M7.2 16.8l2 2'],
  trash: ['M5 7h14', 'M9 7V5h6v2', 'M8 10v8', 'M12 10v8', 'M16 10v8', 'M7 7l1 13h8l1-13'],
  user: ['M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z', 'M4.5 20c1.2-4.1 13.8-4.1 15 0'],
  warning: ['M12 4 21 20H3L12 4Z', 'M12 9v5', 'M12 17h.1'],
};

/**
 * 应用内置描边图标集合(自包含 SVG path,无外部图标资源)。
 * 渲染为 .app-icon 描边图标,样式定义在 styles.css。
 */
export default function AppIcon({ name, className = '', size = 18, style }: AppIconProps) {
  return (
    <svg
      className={`app-icon app-icon-${name} ${className}`.trim()}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      aria-hidden="true"
      focusable="false"
      style={style}
    >
      {iconPaths[name].map((path) => (
        <path key={path} d={path} />
      ))}
    </svg>
  );
}
