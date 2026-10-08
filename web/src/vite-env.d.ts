/// <reference types="vite/client" />

// vite ?worker 导入的类型声明（monaco editor workers）
declare module "*?worker" {
  const workerConstructor: new () => Worker;
  export default workerConstructor;
}
