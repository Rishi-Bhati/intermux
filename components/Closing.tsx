import { INSTALL_COMMAND } from "@/lib/config";
import InstallCommand from "./InstallCommand";
import Logo from "./Logo";
import styles from "./Closing.module.css";

export default function Closing() {
  return (
    <section className={styles.closing}>
      <Logo size={56} />
      <h2 className={styles.title}>Try it on your machine.</h2>
      <InstallCommand command={INSTALL_COMMAND} />
    </section>
  );
}
