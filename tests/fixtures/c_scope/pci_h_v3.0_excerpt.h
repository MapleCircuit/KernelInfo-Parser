/* Excerpt of Linux v3.0 include/linux/pci.h: struct pci_dev (lines 236-344) and struct pci_bus (lines 409-439). */
struct pci_dev {
	struct list_head bus_list;	/* node in per-bus list */
	struct pci_bus	*bus;		/* bus this device is on */
	struct pci_bus	*subordinate;	/* bus this device bridges to */

	void		*sysdata;	/* hook for sys-specific extension */
	struct proc_dir_entry *procent;	/* device entry in /proc/bus/pci */
	struct pci_slot	*slot;		/* Physical slot this device is in */

	unsigned int	devfn;		/* encoded device & function index */
	unsigned short	vendor;
	unsigned short	device;
	unsigned short	subsystem_vendor;
	unsigned short	subsystem_device;
	unsigned int	class;		/* 3 bytes: (base,sub,prog-if) */
	u8		revision;	/* PCI revision, low byte of class word */
	u8		hdr_type;	/* PCI header type (`multi' flag masked out) */
	u8		pcie_cap;	/* PCI-E capability offset */
	u8		pcie_type;	/* PCI-E device/port type */
	u8		rom_base_reg;	/* which config register controls the ROM */
	u8		pin;  		/* which interrupt pin this device uses */

	struct pci_driver *driver;	/* which driver has allocated this device */
	u64		dma_mask;	/* Mask of the bits of bus address this
					   device implements.  Normally this is
					   0xffffffff.  You only need to change
					   this if your device has broken DMA
					   or supports 64-bit transfers.  */

	struct device_dma_parameters dma_parms;

	pci_power_t     current_state;  /* Current operating state. In ACPI-speak,
					   this is D0-D3, D0 being fully functional,
					   and D3 being off. */
	int		pm_cap;		/* PM capability offset in the
					   configuration space */
	unsigned int	pme_support:5;	/* Bitmask of states from which PME#
					   can be generated */
	unsigned int	pme_interrupt:1;
	unsigned int	d1_support:1;	/* Low power state D1 is supported */
	unsigned int	d2_support:1;	/* Low power state D2 is supported */
	unsigned int	no_d1d2:1;	/* Only allow D0 and D3 */
	unsigned int	mmio_always_on:1;	/* disallow turning off io/mem
						   decoding during bar sizing */
	unsigned int	wakeup_prepared:1;
	unsigned int	d3_delay;	/* D3->D0 transition time in ms */

#ifdef CONFIG_PCIEASPM
	struct pcie_link_state	*link_state;	/* ASPM link state. */
#endif

	pci_channel_state_t error_state;	/* current connectivity state */
	struct	device	dev;		/* Generic device interface */

	int		cfg_size;	/* Size of configuration space */

	/*
	 * Instead of touching interrupt line and base address registers
	 * directly, use the values stored here. They might be different!
	 */
	unsigned int	irq;
	struct resource resource[DEVICE_COUNT_RESOURCE]; /* I/O and memory regions + expansion ROMs */
	resource_size_t	fw_addr[DEVICE_COUNT_RESOURCE]; /* FW-assigned addr */

	/* These fields are used by common fixups */
	unsigned int	transparent:1;	/* Transparent PCI bridge */
	unsigned int	multifunction:1;/* Part of multi-function device */
	/* keep track of device state */
	unsigned int	is_added:1;
	unsigned int	is_busmaster:1; /* device is busmaster */
	unsigned int	no_msi:1;	/* device may not use msi */
	unsigned int	block_ucfg_access:1;	/* userspace config space access is blocked */
	unsigned int	broken_parity_status:1;	/* Device generates false positive parity */
	unsigned int	irq_reroute_variant:2;	/* device needs IRQ rerouting variant */
	unsigned int 	msi_enabled:1;
	unsigned int	msix_enabled:1;
	unsigned int	ari_enabled:1;	/* ARI forwarding */
	unsigned int	is_managed:1;
	unsigned int	is_pcie:1;	/* Obsolete. Will be removed.
					   Use pci_is_pcie() instead */
	unsigned int    needs_freset:1; /* Dev requires fundamental reset */
	unsigned int	state_saved:1;
	unsigned int	is_physfn:1;
	unsigned int	is_virtfn:1;
	unsigned int	reset_fn:1;
	unsigned int    is_hotplug_bridge:1;
	unsigned int    __aer_firmware_first_valid:1;
	unsigned int	__aer_firmware_first:1;
	pci_dev_flags_t dev_flags;
	atomic_t	enable_cnt;	/* pci_enable_device has been called */

	u32		saved_config_space[16]; /* config space saved at suspend time */
	struct hlist_head saved_cap_space;
	struct bin_attribute *rom_attr; /* attribute descriptor for sysfs ROM entry */
	int rom_attr_enabled;		/* has display of the rom attribute been enabled? */
	struct bin_attribute *res_attr[DEVICE_COUNT_RESOURCE]; /* sysfs file for resources */
	struct bin_attribute *res_attr_wc[DEVICE_COUNT_RESOURCE]; /* sysfs file for WC mapping of resources */
#ifdef CONFIG_PCI_MSI
	struct list_head msi_list;
#endif
	struct pci_vpd *vpd;
#ifdef CONFIG_PCI_IOV
	union {
		struct pci_sriov *sriov;	/* SR-IOV capability related */
		struct pci_dev *physfn;	/* the PF this VF is associated with */
	};
	struct pci_ats	*ats;	/* Address Translation Service */
#endif
};

struct pci_bus {
	struct list_head node;		/* node in list of buses */
	struct pci_bus	*parent;	/* parent bus this bridge is on */
	struct list_head children;	/* list of child buses */
	struct list_head devices;	/* list of devices on this bus */
	struct pci_dev	*self;		/* bridge device as seen by parent */
	struct list_head slots;		/* list of slots on this bus */
	struct resource *resource[PCI_BRIDGE_RESOURCE_NUM];
	struct list_head resources;	/* address space routed to this bus */

	struct pci_ops	*ops;		/* configuration access functions */
	void		*sysdata;	/* hook for sys-specific extension */
	struct proc_dir_entry *procdir;	/* directory entry in /proc/bus/pci */

	unsigned char	number;		/* bus number */
	unsigned char	primary;	/* number of primary bridge */
	unsigned char	secondary;	/* number of secondary bridge */
	unsigned char	subordinate;	/* max number of subordinate buses */
	unsigned char	max_bus_speed;	/* enum pci_bus_speed */
	unsigned char	cur_bus_speed;	/* enum pci_bus_speed */

	char		name[48];

	unsigned short  bridge_ctl;	/* manage NO_ISA/FBB/et al behaviors */
	pci_bus_flags_t bus_flags;	/* Inherited by child busses */
	struct device		*bridge;
	struct device		dev;
	struct bin_attribute	*legacy_io; /* legacy I/O for this bus */
	struct bin_attribute	*legacy_mem; /* legacy mem */
	unsigned int		is_added:1;
};
