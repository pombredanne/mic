Name:       mic
Summary:    Image Creator for Linux Distributions
Version:    0.19
Release:    0
Group:      System/Utilities
License:    GPL-2.0
BuildArch:  noarch
URL:        http://www.tizen.org
Source0:    %{name}-%{version}.tar.gz
%if 0%{?tizen_version:1}
Source1001: mic.manifest
%endif
Requires:   python-rpm
Requires:   util-linux
Requires:   coreutils
Requires:   python >= 2.5
Requires:   e2fsprogs
Requires:   dosfstools >= 2.11-8
Requires:   syslinux >= 3.82
Requires:   kpartx
Requires:   parted
Requires:   device-mapper
Requires:   /usr/bin/genisoimage
Requires:   cpio
#Requires:   isomd5sum
Requires:   gzip
Requires:   bzip2
Requires:   python-urlgrabber
Requires:   yum >= 3.2.24
%if ! 0%{?centos_version}
%if 0%{?suse_version}
Requires:   btrfsprogs
%else
Requires:   btrfs-progs
%endif
%endif

%if 0%{?suse_version}
Requires:   squashfs >= 4.0
Requires:   python-m2crypto
%else
Requires:   squashfs >= 4.0
Requires:   python-M2Crypto
%endif

%if 0%{?fedora_version} || 0%{?centos_version}
Requires:   syslinux-extlinux
%endif

%if 0%{?tizen_version:1}
Requires:   qemu-linux-user
%else
Requires:   qemu-arm-static
%endif

Requires:   python-zypp

BuildRequires:  python-devel
%if ! 0%{?tizen_version:1}
BuildRequires:  python-docutils
%endif

%if ! 0%{?centos_version}
BuildRequires:fdupes
%endif

%description
The tool mic is used to create and manipulate images for Linux distributions.
It is composed of three subcommand\: create, convert, chroot. Subcommand create
is used to create images with different types; subcommand convert is used to
convert an image to a specified type; subcommand chroot is used to chroot into
an image.

%prep
%setup -q -n %{name}-%{version}
%if 0%{?tizen_version:1}
cp %{SOURCE1001} .
%endif

%build
CFLAGS="$RPM_OPT_FLAGS" %{__python} setup.py build
%if ! 0%{?tizen_version:1}
%__make man
%endif

%install
rm -rf %{buildroot}
%if 0%{?suse_version}
%{__python} setup.py install --root=%{buildroot} --prefix=%{_prefix}
%else
%{__python} setup.py install --root=%{buildroot} -O1
%endif

# install man page
mkdir -p %{buildroot}%{_mandir}/man1
%if ! 0%{?tizen_version:1}
install -m644 doc/mic.1 %{buildroot}%{_mandir}/man1
%endif

%if ! 0%{?centos_version}
%fdupes %{buildroot}
%endif


%files
%if 0%{?tizen_version:1}
%manifest %{name}.manifest
%endif
%defattr(-,root,root,-)
%if ! (0%{?suse_version} || 0%{?centos_version})
%license COPYING
%endif
%doc doc/*
%doc README.rst AUTHORS ChangeLog
%if ! 0%{?tizen_version:1}
%{_mandir}/man1/*
%endif
%dir %{_sysconfdir}/%{name}
%config(noreplace) %{_sysconfdir}/%{name}/%{name}.conf
%{python_sitelib}/*
%dir %{_prefix}/lib/%{name}
%{_prefix}/lib/%{name}/*
%{_bindir}/*
