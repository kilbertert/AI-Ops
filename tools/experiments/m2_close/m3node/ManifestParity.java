import com.trendpower.aiops.node.exec.ExecManifest;

import java.nio.file.Path;

/**
 * 跨语言清单对照的 Java 一侧：**同一组样本**、同一个真实加载器。
 *
 * 它不重写判据，只把 {@link ExecManifest#load(Path)} 的结论打出来，由脚本与
 * Gateway 侧的 Python 结果逐条比对。判定放在脚本里，避免「两边各自写一份期望」。
 */
public final class ManifestParity {

    private ManifestParity() {
    }

    public static void main(String[] args) {
        for (String arg : args) {
            Path path = Path.of(arg);
            String verdict;
            try {
                ExecManifest manifest = ExecManifest.load(path);
                verdict = (manifest == null ? "DISABLED" : "ACCEPTED:" + manifest.id());
            } catch (RuntimeException e) {
                verdict = "REJECTED";
            }
            System.out.println(path.getFileName() + "=" + verdict);
        }
    }
}
