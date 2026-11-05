// GitHub runner先发SIGINT再发SIGTERM；让SDK先走finally关闭本次容器。
const controller = new AbortController();
for (const signal of ['SIGINT', 'SIGTERM'] as const) {
  process.once(signal, () => controller.abort(new Error(`Agent cancelled: ${signal}`)));
}
export const cancellationSignal = controller.signal;
